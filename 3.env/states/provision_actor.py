"""为 L3 黄金路径准备一个真实操作者：一个超级用户 + 一个 API Token。

不是 mock：后续每一步 HTTP 请求都用这个 token 走真实的 DRF 认证链
（ApiTokenAuthentication → 权限 → 视图 → ORM → sqlite）。

用法（在被测仓 src/backend/InvenTree 下执行）:
    python manage.py shell -c "exec(open(r'<本文件路径>').read())"
"""

from django.contrib.auth import get_user_model

from users.models import ApiToken

User = get_user_model()

USERNAME = "flywheel-actor"

user, created = User.objects.get_or_create(
    username=USERNAME,
    defaults={"email": "flywheel@example.invalid", "is_staff": True, "is_superuser": True},
)
if created:
    user.set_password("flywheel-local-only")
    user.save()

token = ApiToken.objects.filter(user=user, name="flywheel-l3").first()
if token is None:
    token = ApiToken.objects.create(user=user, name="flywheel-l3")

print(f"KS_ACTOR\t{USERNAME}\tcreated={created}\ttoken={token.key}")
