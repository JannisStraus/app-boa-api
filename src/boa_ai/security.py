import base64
import os
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.security.http import (
    HTTPAuthorizationCredentials,
    HTTPBase,
    HTTPBearerModel
)
from fastapi.security.utils import get_authorization_scheme_param
import jwt
from starlette.status import HTTP_401_UNAUTHORIZED


class SHIPAuth(HTTPBase):
    def __init__(
            self,
            *,
            bearerFormat: Optional[str] = None,
            scheme_name: Optional[str] = None,
            auto_error: bool = True,
    ):
        self.model = HTTPBearerModel(bearerFormat=bearerFormat)
        self.scheme_name = scheme_name or self.__class__.__name__
        self.auto_error = auto_error

    async def __call__(
            self, request: Request
    ) -> Optional[HTTPAuthorizationCredentials]:
        # Try to get the JWT from HTTP Authorization header
        authorization: Optional[str] = request.headers.get('Authorization')
        if authorization:
            scheme, credentials = get_authorization_scheme_param(authorization)

            if not (authorization and scheme and credentials):
                if self.auto_error:
                    raise HTTPException(
                        status_code=HTTP_401_UNAUTHORIZED,
                        detail='Not authenticated'
                    )
                return None

            if scheme.lower() != 'bearer':
                if self.auto_error:
                    raise HTTPException(
                        status_code=HTTP_401_UNAUTHORIZED,
                        detail='Invalid authentication credentials',
                    )
                return None

        # If not already available, try to get the JWT from cookie
        if not authorization:
            authorization = request.cookies.get('shipToken')
            if not authorization:
                if self.auto_error:
                    raise HTTPException(
                        status_code=HTTP_401_UNAUTHORIZED,
                        detail='Not authenticated'
                    )
                return None
            scheme = ''
            credentials = authorization

        # If we have access to the signing key, then validate the JWT
        if 'SHIPAUTH' in os.environ:
            try:
                jwt.decode(
                    jwt=credentials,
                    key=base64.b64decode(os.environ['SHIPAUTH'])
                )
            except (jwt.DecodeError, jwt.ExpiredSignatureError) as error:
                if self.auto_error:
                    raise HTTPException(
                        status_code=HTTP_401_UNAUTHORIZED,
                        detail=f'Expired/Invalid JWT provided: {str(error)}'
                    ) from error
                return None

        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)
