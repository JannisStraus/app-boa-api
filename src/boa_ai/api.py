import io
import re
import os
import pathlib
import tempfile
from typing import List
import urllib.parse
import uuid
import json
from dicomweb_client.api import DICOMwebClient
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response, StreamingResponse, HTMLResponse
from fastapi.security.http import HTTPAuthorizationCredentials
import pydicom
import pydicom_seg
import SimpleITK as sitk

from boa_ai.worker.tasks import app as celery_app, run_pipeline_for_series
from boa_ai.s3 import client as s3_client
from boa_ai.security import SHIPAuth

HTML_401_RESPONSE = """<!DOCTYPE html>
<html lang="de">

<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta charset="utf-8">
<title>SHIP | Login benötigt</title>
</head>

<body>
<header>
<h1>Login benötigt</h1>
</header>
<main>
Sie werden zum <a href="/app/Auth">Login-Formular</a> weitergeleitet.
</main>
<script type="application/javascript">
window.setTimeout(function(){ window.location = "/app/Auth"; }, 0)
</script>
</body>
</html>"""

UID_REGEX = r'\d+(\.\d+)'

tags_metadata = [
    {
        "name": "study_information",
        "description": "Get information about a study ID.",
    },
    {
        "name": "task_handling",
        "description": "Create a task, get information about a particular task or delete a task.",
    },
    {
        "name": "downloads",
        "description": "Download the results of the body composition analysis.",
    },
]

app = FastAPI(
    title='Body Composition Analysis',
    description='Computes the body composition analysis of Whole Body, Abdomen and Thorax CT scans.',
    version='0.1.0',
    openapi_tags=tags_metadata
)
token_security = SHIPAuth()


@app.exception_handler(HTTPException)
async def ship_auth_unauthorized_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        accept = request.headers.get('Accept')
        if accept == 'text/html':
            return HTMLResponse(
                content=HTML_401_RESPONSE,
                status_code=401,
                headers={'Referer': urllib.parse.quote(str(request.url))}
            )
    return await http_exception_handler(request, exc)


# I guess for liveness probe (kubernetes)
@app.get('/alive', include_in_schema=False)
def alive():
    pass


# Not sure what this is for, maybe for Sina? Given an accession number,
# it finds the respective study ID and the available series connected to the study
# then it runs the pipeline on it
# TODO: Do we need this?
@app.get('/ris/{accession_number}', include_in_schema=False)
def ris_call(accession_number: int,
             auth: HTTPAuthorizationCredentials = Depends(token_security)):
    client = DICOMwebClient(
        f'{os.environ["SHIP_URL"]}/app/DicomWeb/GEPACS',
        headers={'Authorization': f'Bearer {auth.credentials}'}
    )
    possible_studies = client.search_for_studies(
        search_filters={'AccessionNumber': accession_number}
    )
    if not possible_studies:
        raise HTTPException(404, f'No study found for accession number {accession_number}')

    available_series = _filter_series(
        client=client,
        study_instance_uids=[
            pydicom.Dataset.from_json(x).StudyInstanceUID
            for x in possible_studies
        ]
    )
    if not available_series:
        raise HTTPException(404,
                            f'No matching series found for accession number {accession_number}')

    # TODO Need to store results directly into PACS instead of S3
    run_pipeline_for_series.delay(
        pacs='GEPACS',
        study_instance_uid=available_series[0]['study_instance_uid'],
        series_instance_uid=available_series[0]['series_instance_uid'],
        token=auth.credentials
    )


# Needs authentication because it collects information from the PACS
# Return the available series given a study ID and a pacs
@app.get(
    '/{pacs}/studies/{study_instance_uid}/series',
    tags=['study_information']
)
def suggest_series(pacs: str,
                   study_instance_uid: str = Path(..., regex=UID_REGEX),
                   auth: HTTPAuthorizationCredentials = Depends(token_security)):
    """
    Given a database name and a study UID, it returns a list of series on which it is possible to 
    perform a body composition analysis.
    """
    client = DICOMwebClient(
        f'{os.environ["SHIP_URL"]}/app/DicomWeb/{pacs}',
        headers={'Authorization': f'Bearer {auth.credentials}'}
    )
    available_series = _filter_series(client, [study_instance_uid])
    return available_series


def _filter_series(client: DICOMwebClient, study_instance_uids: List[str]):
    result = []
    for study_instance_uid in study_instance_uids:
        possible_series = client.search_for_series(
            study_instance_uid=study_instance_uid,
            fields=['SeriesDescription'],
            search_filters={'Modality': 'CT'},  # TODO: Check if this works as expected
        )
        for series in possible_series:
            series = pydicom.Dataset.from_json(series)
            description = series.SeriesDescription
            # TODO: Check if this works as expected
            if re.match(
                    r'(CT\s*)?(Abd\w*|Th\w*|WB|)\.?'
                    r'(\s*(art\w*|PORT\w*|nativ\w*|ven\w*|KM)\.?.*)?'
                    r'\s+?([0-4](\.[0-9])?|5(.0)?)(mm)?.*'
                    r'((B|Br|H|Hr|I|J)([23][0-9]|4[01])[fs]?)($|\s).*',
                    description):
                result.append({
                    'study_instance_uid': study_instance_uid,
                    'series_instance_uid': series.SeriesInstanceUID,
                    'series_description': series.SeriesDescription
                })
    return result


# Needs authentication because it collects information from the PACS
# Runs pipeline on given pacs, study and series
@app.post(
    '/{pacs}/studies/{study_instance_uid}/series/{series_instance_uid}',
    tags=['task_handling']
)
def create_task(pacs: str,
                study_instance_uid: str = Path(..., regex=UID_REGEX),
                series_instance_uid: str = Path(..., regex=UID_REGEX),
                auth: HTTPAuthorizationCredentials = Depends(token_security)):
    """
    Runs the body composition analysis pipeline on the given series and returns a task to retrieve
    the results.
    """
    task = run_pipeline_for_series.delay(
        pacs=pacs,
        study_instance_uid=study_instance_uid,
        series_instance_uid=series_instance_uid,
        token=auth.credentials
    )

    return {'task_id': task.id}


# Returns information about the status of the pipeline run (e.g. STARTED, SUCCESS, FAILURE)
@app.get(
    '/tasks/{task_id}',
    tags=['task_handling']
)
def get_task_info(task_id: uuid.UUID):
    """
    Returns the status of the task and some information about it.
    """
    result = celery_app.AsyncResult(str(task_id))
    info = {}
    if result.successful():
        info = result.info
    elif result.failed():
        info = {'message': str(result.result)}
    return {'status': result.status, 'info': info}


# Return the report for a task_id
@app.get(
    '/tasks/{task_id}/report',
    response_class=StreamingResponse,
    responses={
        200: {
            'content': {
                'application/dicom': {},
                'application/pdf': {},
            },
            'description': 'Return a DICOM or a PDF of the report.',
        },
    },
    tags=['downloads']
)
def download_report(task_id: uuid.UUID,
                    accept: str = Header('application/pdf')):
    """
    Downloads the body composition analysis report.
    """
    s3_file, filename = _check_s3_and_return_output_name(task_id, 'report')
    if accept == 'application/dicom':
        return StreamingResponse(
            content=s3_file,
            media_type='application/dicom',
            headers={'Content-Disposition': f'inline; filename={filename}.dcm'}
        )

    dcm = pydicom.dcmread(io.BytesIO(s3_file.read()))
    return Response(
        content=dcm.EncapsulatedDocument,
        media_type='application/pdf',
        headers={'Content-Disposition': f'inline; filename={filename}.pdf'}
    )


# Return the body parts for a task_id
@app.get(
    '/tasks/{task_id}/body_parts',
    response_class=StreamingResponse,
    responses={
        200: {
            'content': {
                'application/dicom': {},
                'image/gznii': {},
            },
            'description': 'Return a DICOM or a compressed NIfTI of the body parts segmentation.',
        },
    },
    tags=['downloads']
)
def download_body_parts(task_id: uuid.UUID,
                          accept: str = Header('image/gznii')):
    """
    Downloads the body region segmentation.
    """
    return _download_output(task_id, 'body_parts', accept)


# Return the body regions for a task_id
@app.get(
    '/tasks/{task_id}/body_regions',
    response_class=StreamingResponse,
    responses={
        200: {
            'content': {
                'application/dicom': {},
                'image/gznii': {},
            },
            'description': 'Return a DICOM or a compressed NIfTI of the body region segmentation.',
        },
    },
    tags=['downloads']
)
def download_body_regions(task_id: uuid.UUID,
                          accept: str = Header('image/gznii')):
    """
    Downloads the body region segmentation.
    """
    return _download_output(task_id, 'body_regions', accept)


# Return the tissues for a task_id
@app.get(
    '/tasks/{task_id}/tissues',
    response_class=StreamingResponse,
    responses={
        200: {
            'content': {
                'application/dicom': {},
                'image/gznii': {},
            },
            'description': 'Return a DICOM or a compressed NIfTI of the tissue segmentation.',
        },
    },
    tags=['downloads']
)
def download_tissues(task_id: uuid.UUID,
                     accept: str = Header('image/gznii')):
    """
    Downloads the tissue segmentation.
    """
    return _download_output(task_id, 'tissues', accept)


# Return the total segmentation for a task_id
@app.get(
    '/tasks/{task_id}/total',
    response_class=StreamingResponse,
    responses={
        200: {
            'content': {
                'application/dicom': {},
                'image/gznii': {},
            },
            'description': 'Return a DICOM or a compressed NIfTI of the tissue segmentation.',
        },
    },
    tags=['downloads']
)
def download_total(task_id: uuid.UUID,
                     accept: str = Header('image/gznii')):
    """
    Downloads the tissue segmentation.
    """
    return _download_output(task_id, 'total', accept)


def _download_output(task_id: uuid.UUID,
                     output_type: str,
                     accept: str = Header('image/gznii')):
    """
    Helper function used by download_tissues and download_body_regions to either stream the saved
    DICOMs or to convert them to compressed NIfTIs.
    """
    s3_file, filename = _check_s3_and_return_output_name(task_id, output_type)
    if accept == 'application/dicom':
        return StreamingResponse(
            content=s3_file, media_type='application/dicom',
            headers={'Content-Disposition': f'inline; filename={filename}.dcm'})

    # TODO Maybe optimize this bit of conversion
    with tempfile.TemporaryDirectory() as working_dir:
        working_dir = pathlib.Path(working_dir)
        with (working_dir / (output_type + '.dcm')).open('wb') as ofile:
            ofile.write(s3_file.read())
        dcm = pydicom.dcmread(working_dir / (output_type + '.dcm'))
        image = pydicom_seg.MultiClassReader().read(dcm).image
        sitk.WriteImage(image, str(working_dir / (output_type + '.nii.gz')), True)
        with (working_dir / (output_type + '.nii.gz')).open('rb') as ifile:
            content = ifile.read()
    return Response(content=content, media_type='image/gznii',
                    headers={'Content-Disposition': f'inline; filename={filename}.nii.gz'})


def _check_s3_and_return_output_name(task_id: uuid.UUID, output_type: str):
    """
    Helper function used by all download methods to check if the requested task ID exists in S3
    and returns the s3 file together with a file name for the output.
    """
    s3_key = f'bca/files/{task_id}/{output_type}'
    if not s3_client.exists(s3_key):
        raise HTTPException(status_code=404)

    s3_file = s3_client.read_stream(s3_key)
    inputs = get_task_info(task_id).get('info').get('input')
    filename = f'{inputs["study_instance_uid"]}_{inputs["series_instance_uid"]}_{output_type}'
    return s3_file, filename


# Return the measurements for a task_id as json
@app.get(
    '/tasks/{task_id}/json',
    tags=['downloads']
)
def retrieve_measurements(task_id: uuid.UUID):
    """
    Returns a JSON with body composition analysis information.
    """
    s3_key = f'bca/files/{task_id}/json'
    if not s3_client.exists(s3_key):
        raise HTTPException(status_code=404)

    s3_file = s3_client.read_stream(s3_key)
    return json.loads(s3_file.read().decode('utf-8'))


# Return the measurements for a task_id as csv
@app.get(
    '/tasks/{task_id}/measurements',
    response_class=StreamingResponse,
    responses={
        200: {
            'content': {
                'application/xlsx': {},
            },
            'description': 'Return a CSV or a XLSX with some CT volume information.',
        },
    },
    tags=['downloads']
)
def download_measurements(task_id: uuid.UUID):
    """
    Downloads some aggreagated information of the body composition analysis.
    """
    s3_file, filename = _check_s3_and_return_output_name(task_id, 'measurements')
    return StreamingResponse(content=s3_file, media_type='application/xlsx',
                             headers={'Content-Disposition': f'inline; filename='
                                                             f'{filename}.xlsx'})


# Remove the task
@app.delete(
    '/tasks/{task_id}',
    tags=['task_handling']
)
def delete_task(task_id: uuid.UUID):
    """
    Deletes the task and its associated storage.
    """
    s3_client.delete_files(f'bca/files/{task_id}/')
    result = celery_app.AsyncResult(str(task_id))
    print(result.id, type(result.id))
    result = result.forget()
