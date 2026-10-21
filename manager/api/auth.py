"""
Bearer-token authentication for the JSON API (slice 15, E11).

`Authorization: Bearer <token>`; the token is an ApiToken row (hash of the
secret, shown once at creation in the admin) and acts as its user, so the
viewer/operator/admin groups apply. Every endpoint needs a valid token:
no anonymous access, not even on the LAN. No cookies, no CSRF.
"""

from datetime import timedelta
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from registry.models import ApiToken

LAST_USED_RESOLUTION = timedelta(seconds=60)   # one write per minute per token, not per request


def error(status: int, message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def token_from(request):
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def authenticate(request):
    """The ApiToken for the request's bearer token, or None."""
    raw = token_from(request)
    if raw is None:
        return None
    token = ApiToken.authenticate(raw)
    if token is None:
        return None
    now = timezone.now()
    if token.last_used_at is None or now - token.last_used_at > LAST_USED_RESOLUTION:
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return token


def api_view(perm=None, methods=("GET",)):
    """Method check, token, permission; the view gets request.api_token and request.api_user."""

    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = error(405, f"method not allowed; use {', '.join(methods)}")
                response["Allow"] = ", ".join(methods)
                return response
            token = authenticate(request)
            if token is None:
                response = error(401, "a valid API token is required: Authorization: Bearer <token>")
                response["WWW-Authenticate"] = 'Bearer realm="dbsync-manager"'
                return response
            if perm and not token.user.has_perm(perm):
                return error(403, f"token '{token.name}' (user {token.user.get_username()}) lacks {perm}")
            request.api_token = token
            request.api_user = token.user
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
