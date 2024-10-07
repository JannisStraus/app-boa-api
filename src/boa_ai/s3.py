import os

import boto3


class Storage:
    def __init__(self, root_bucket: str, s3_url: str, s3_access_key: str, s3_secret_key: str):
        self._s3 = boto3.resource(
            service_name='s3',
            endpoint_url=s3_url,
            aws_access_key_id=s3_access_key,
            aws_secret_access_key=s3_secret_key,
            config=boto3.session.Config(
                signature_version='s3v4'
            )
        )

        # Configure jobs bucket
        self._bucket = self._s3.Bucket(root_bucket)
        if not self._bucket.creation_date:
            self._bucket.create()
        self._bucket.LifecycleConfiguration().put(LifecycleConfiguration={
            'Rules': [
                {
                    'ID': 'AutoCleanUpJobs',
                    'Expiration': {
                        'Days': 1,
                    },
                    'Filter': {'Prefix': ''},
                    'Status': 'Enabled'
                }
            ]
        })

    def exists(self, name: str) -> bool:
        objects = list(self._bucket.objects.filter(Prefix=name))
        return len(objects) > 0

    def store(self, name: str, content: bytes):
        self._s3.Object(self._bucket.name, name).put(
            Body=content
        )

    def read_stream(self, name: str):
        return self._s3.Object(self._bucket.name, name).get()['Body']

    def delete_files(self, prefix: str):
        self._bucket.objects.filter(Prefix=prefix).delete()


client = Storage(
    os.environ['S3_BUCKET'],
    os.environ['S3_ENDPOINT_URL'],
    os.environ['S3_ACCESS_KEY_ID'],
    os.environ['S3_SECRET_ACCESS_KEY'],    
)
