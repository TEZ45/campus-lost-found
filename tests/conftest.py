import pytest


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings_parallel_suffix, tmp_path_factory):
    from django.conf import settings

    database = settings.DATABASES["default"]
    if database["ENGINE"] == "django.db.backends.sqlite3":
        database.setdefault("TEST", {})["NAME"] = str(
            tmp_path_factory.mktemp("database") / "test.sqlite3"
        )


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"


@pytest.fixture
def users(db):
    from django.contrib.auth.models import User

    from apps.reports.models import UserProfile

    users = {name: User.objects.create_user(name) for name in (
        "owner", "claimant", "other", "service", "admin", "service_admin",
    )}
    for name in ("service", "service_admin"):
        UserProfile.objects.create(user=users[name], is_service_staff=True)
    for name in ("admin", "service_admin"):
        users[name].is_staff = True
        users[name].is_superuser = True
        users[name].save()
    return users
