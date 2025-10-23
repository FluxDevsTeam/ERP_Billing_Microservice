from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.models import TokenUser
from rest_framework_simplejwt.exceptions import InvalidToken


class CustomTokenUser(TokenUser):
    def __str__(self):
        return getattr(self, 'email', str(self.id))


class CustomJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        try:
            print("Using CustomJWTAuthentication with CustomTokenUser")
            return CustomTokenUser(validated_token)
        except KeyError as e:
            raise InvalidToken(f"Token missing required claim: {str(e)}")