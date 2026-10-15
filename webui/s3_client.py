"""Standard S3 client for the internal, passwordless Autumn endpoint."""
from functools import lru_cache

import boto3
from botocore import UNSIGNED
from botocore.config import Config


@lru_cache(maxsize=4)
def client(endpoint_url: str):
    return boto3.session.Session().client(
        "s3", endpoint_url=endpoint_url, region_name="us-east-1",
        config=Config(
            signature_version=UNSIGNED,
            s3={"addressing_style": "path"},
            connect_timeout=10, read_timeout=120,
            retries={"mode": "standard", "total_max_attempts": 3},
            # Autumn supports Content-MD5. Do not request optional AWS checksum
            # trailers or CRC response validation from this S3-compatible server.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def object_args(object_key: str) -> dict[str, str]:
    bucket, separator, key = object_key.partition("/")
    if not bucket or not separator or not key:
        raise ValueError("S3 object must contain bucket/key")
    return {"Bucket": bucket, "Key": key}
