from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    u = request.user
    return Response({
        "sub": u.sub, "email": u.email, "full_name": u.full_name, "role": u.role,
        "phone": u.phone, "farmer_id": u.farmer_id, "national_id": u.national_id,
        "company_name": u.company_name, "is_staff": u.is_staff_role,
    })
