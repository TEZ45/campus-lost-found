from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import OperationalError, connection, connections, transaction

from apps.reports.models import Category

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the PostgreSQL test database.")


def test_postgresql_cancels_slow_statements_and_connection_recovers():
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '100ms'")
        with (
            pytest.raises(OperationalError, match="statement timeout"),
            transaction.atomic(), connection.cursor() as cursor,
        ):
            cursor.execute("SELECT pg_sleep(5)")
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)


def test_postgresql_lock_timeout_preserves_the_locked_row():
    category = Category.objects.create(name="Lock timeout fixture")
    locked, release = Event(), Event()

    def hold_row():
        try:
            with transaction.atomic():
                Category.objects.select_for_update().get(pk=category.pk)
                locked.set()
                assert release.wait(timeout=10)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_row)
        try:
            assert locked.wait(timeout=10)
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '100ms'")
                with pytest.raises(OperationalError, match="lock timeout"), transaction.atomic():
                    Category.objects.filter(pk=category.pk).update(name="Unexpected overwrite")
        finally:
            release.set()
            holder.result(timeout=10)
    category.refresh_from_db()
    assert category.name == "Lock timeout fixture"
