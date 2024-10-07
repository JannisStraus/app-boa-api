import io
import logging
import os
import pathlib
import subprocess
import tempfile
from typing import List, Tuple, Dict, Any
import json
from celery import Celery
from dicomweb_client.api import DICOMwebClient
import numpy as np
import pydicom
import pydicom_seg
import SimpleITK as sitk
import pandas as pd
from datetime import datetime
from boa_ai.s3 import client as s3_client
from boa_ai.worker.exceptions import *

from body_organ_analysis.commands import analyze_ct

# HACK Patch celery S3 result deletion
import celery.backends.s3


def _fix_type_error_bug(self, key):
    from kombu.utils.encoding import bytes_to_str
    key = bytes_to_str(key)
    s3_object = self._get_s3_object(key)
    s3_object.delete()


celery.backends.s3.S3Backend.delete = _fix_type_error_bug
app = Celery(
    backend='s3',
    broker=os.environ['CELERY_BROKER'],
)

app.conf.update(
    s3_access_key_id=os.environ['S3_ACCESS_KEY_ID'],
    s3_secret_access_key=os.environ['S3_SECRET_ACCESS_KEY'],
    s3_endpoint_url=os.environ['S3_ENDPOINT_URL'],
    s3_bucket=os.environ['S3_BUCKET'],
    s3_base_path='bca/celery/',
    task_track_started=True,
)


@app.task
def run_pipeline_for_series(pacs: str,
                            study_instance_uid: str,
                            series_instance_uid: str,
                            token: str):
    task_id = run_pipeline_for_series.request.id
    with tempfile.TemporaryDirectory() as working_dir:
        working_dir = pathlib.Path(working_dir)
        (working_dir / 'input').mkdir()
        (working_dir / 'output').mkdir()

        # Run pipeline
        try:
            _download_series(working_dir, pacs, study_instance_uid, series_instance_uid, token)
        except:
            raise DicomWebError(
                f'Failed to retrieve {study_instance_uid}/{series_instance_uid} from {pacs}'
            )
        image, dicom_files = _load_series_from_disk(working_dir)
        # Save image to temporary folder
        input_path = working_dir / 'input' / 'image.nii.gz'
        output_dir = working_dir / 'output'
        sitk.WriteImage(image, str(input_path), True)

        analyze_ct(
            input_folder=input_path,
            processed_output_folder=output_dir,
            excel_output_folder=output_dir,
            models=["bca", "total"],
            nr_thr_saving=1,
        )

        file_list = sorted(output_dir.iterdir())
        print(file_list)

        # Load JSON Data
        json_data = json.load((output_dir / 'bca-measurements.json').open('r'))
        # Add JSON data
        s3_client.store(f'bca/files/{task_id}/json',
                        bytes(json.dumps(json_data).encode('UTF-8')))

        # Additionally save data as Excel
        output_df = pd.ExcelFile(output_dir / 'output.xlsx')
        with io.BytesIO() as buffer:
            with pd.ExcelWriter(buffer) as writer:
                for sheet_name in output_df.sheet_names:
                    df = pd.read_excel(output_dir / "output.xlsx", sheet_name=sheet_name)
                    df.to_excel(output_dir / f"{sheet_name}.xlsx", index=False)
            s3_client.store(f'bca/files/{task_id}/measurements', buffer.getvalue())

        # Write encapsulated PDF DICOM
        subprocess.call([
            'pdf2dcm',
            '--study-from', dicom_files[0],
            '--title', 'Body and Organ Analysis Report',
            str(working_dir / 'output' / 'report.pdf'),
            str(working_dir / 'output' / 'report.dcm')
        ])
        timestamp = datetime.now()
        # Save the outputs information of the DICOMs in a list of dictionaries
        output_dcm_dicts = []
        # TODO: Currently the JSON/CSV are not saved as DICOMs, should they be?
        for i, out_name in enumerate(['report', 'tissues', 'body-regions', 'body-parts', 'total']):
            if out_name == 'report':
                out_dcm = pydicom.dcmread(output_dir / 'report.dcm')
                out_dcm.SeriesDescription = 'Body Composition Analysis Report'
            else:
                nifti_seg = sitk.ReadImage(
                    str(output_dir / f"{out_name}.nii.gz")
                )
                resampled_seg = sitk.Resample(
                    nifti_seg, image, sitk.Transform(), sitk.sitkNearestNeighbor
                )

                resampled_arr = sitk.GetArrayFromImage(resampled_seg)
                resampled_arr[resampled_arr == 255] = 0
                resampled_seg_new = sitk.GetImageFromArray(resampled_arr)
                resampled_seg_new.CopyInformation(resampled_seg)

                # Write DICOM-SEG from ITK image
                json_file = (pathlib.Path(__file__).parent / (out_name + '-meta.json')).open('r')
                json_template = json.load(json_file)
                json_template['BodyPartExamined'] = _transform_examined_body_part_name(
                    json_data['body_parts'])
                template = pydicom_seg.template.from_dcmqi_metainfo(json_template)
                json_file.close()
                writer = pydicom_seg.MultiClassWriter(
                    template=template,
                    inplane_cropping=False,
                    skip_empty_slices=True,
                    skip_missing_segment=False
                )
                out_dcm = writer.write(resampled_seg_new, dicom_files)

            out_dcm.SeriesNumber = 42000 + i  # Add enumerate to have different values
            out_dcm.SeriesInstanceUID = pydicom.uid.generate_uid(
                entropy_srcs=[study_instance_uid, series_instance_uid, out_name]
            )
            out_dcm.SOPInstanceUID = pydicom.uid.generate_uid(
                entropy_srcs=[study_instance_uid, out_dcm.SeriesInstanceUID]
            )
            out_dcm.file_meta.MediaStorageSOPInstanceUID = out_dcm.SOPInstanceUID
            _set_timestamp(out_dcm, timestamp)
            # Remove the replaces when the API endpoints will be changed
            output_dcm_dicts.append({
                'name': out_name.replace('-', '_'),
                'study_instance_uid': study_instance_uid,
                'series_instance_uid': out_dcm.SeriesInstanceUID,
                'sop_instance_uid': out_dcm.SOPInstanceUID
            })
            with io.BytesIO() as buffer:
                out_dcm.save_as(buffer)
                buffer.seek(0)
                s3_client.store(f'bca/files/{task_id}/' + out_name.replace('-', '_'), buffer.read())

    return {
        'input': {
            'pacs': pacs,
            'study_instance_uid': study_instance_uid,
            'series_instance_uid': series_instance_uid
        },
        'output': output_dcm_dicts
    }


def _set_timestamp(dcm: pydicom.Dataset, timestamp) -> None:
    dcm.InstanceCreationDate = timestamp.strftime("%Y%m%d")
    dcm.InstanceCreationTime = timestamp.strftime("%H%M%S.%f")
    dcm.SeriesDate = dcm.InstanceCreationDate
    dcm.SeriesTime = dcm.InstanceCreationTime
    dcm.ContentDate = dcm.InstanceCreationDate
    dcm.ContentTime = dcm.InstanceCreationTime


def _transform_examined_body_part_name(examined_body_parts):
    body_str = '|'.join(part.upper()[:3]
                        for part, present in examined_body_parts.items() if present)
    return body_str


def _download_series(working_dir: pathlib.Path,
                     pacs: str,
                     study_instance_uid: str,
                     series_instance_uid: str,
                     token: str):
    client = DICOMwebClient(
        f'{os.environ["SHIP_URL"]}/app/DicomWeb/{pacs}',
        headers={'Authorization': f'Bearer {token}'}
    )
    num_instances = 0
    dicom_iter = client.iter_series(study_instance_uid, series_instance_uid)
    for num_instances, instance in enumerate(dicom_iter, 1):
        logging.debug('Retrieved DICOM with SOPInstanceUID=%s', instance.SOPInstanceUID)
        instance.save_as(working_dir / 'input' / f'{instance.SOPInstanceUID}.dcm')

    logging.info(
        'Fetched %d DICOM instances from %s with StudyInstanceUID=%s and SeriesInstanceUID=%s',
        num_instances, pacs, study_instance_uid, series_instance_uid
    )


def _load_series_from_disk(working_dir: pathlib.Path) -> Tuple[sitk.Image, List[str]]:
    reader = sitk.ImageSeriesReader()
    files = reader.GetGDCMSeriesFileNames(str(working_dir / 'input'))
    reader.SetFileNames(files)
    image = reader.Execute()
    return image, files
