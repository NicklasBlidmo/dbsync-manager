from django.db import DatabaseError, connection
from django.http import JsonResponse


def healthz(request):
    """Liveness for the container healthcheck: web up and registry reachable."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError as exc:
        return JsonResponse({"status": "error", "database": str(exc)}, status=503)
    return JsonResponse({"status": "ok"})
