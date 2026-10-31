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
if token is not None:
    # v2 token 的明文 secret 只在创建瞬间存在（库里只有 identifier + hmac），
    # 所以要拿明文必须重建一次。
    token.delete()
    token = None
if token is None:
    token = ApiToken.objects.create(user=user, name="flywheel-l3")

raw = getattr(token, "_raw_secret", None)
if not raw:
    raise SystemExit("无法取得 token 明文：上游 v2 token 未暴露 _raw_secret")

print(f"KS_ACTOR\t{USERNAME}\tcreated={created}\ttoken={raw}")
