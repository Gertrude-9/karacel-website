"""
Smoke tests for Karacel Association.
Verifies public pages, auth protection, and role-based access.
"""
import pytest
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ============================================================
# APP-WIDE
# ============================================================
def test_app_imports():
    assert app is not None


# ============================================================
# PUBLIC PAGES — must load without login
# ============================================================
class TestPublicPages:
    def test_splash(self, client):
        assert client.get("/").status_code in (200, 302)

    def test_home(self, client):
        assert client.get("/home").status_code in (200, 302)

    def test_about(self, client):
        assert client.get("/about").status_code == 200

    def test_contact(self, client):
        assert client.get("/contact").status_code == 200

    def test_savings(self, client):
        assert client.get("/savings").status_code == 200

    def test_credit(self, client):
        assert client.get("/credit").status_code == 200

    def test_welfare(self, client):
        assert client.get("/welfare").status_code == 200

    def test_login_page(self, client):
        assert client.get("/login").status_code == 200


# ============================================================
# AUTH PROTECTION — must redirect when not logged in
# ============================================================
class TestAuthProtection:
    def test_admin_dashboard(self, client):
        assert client.get("/admin/dashboard").status_code == 302

    def test_admin_manage_users(self, client):
        assert client.get("/admin/users/manage").status_code == 302

    def test_admin_archives(self, client):
        assert client.get("/admin/archives").status_code == 302

    def test_admin_year_end(self, client):
        assert client.get("/admin/year-end/preview").status_code == 302

    def test_treasurer_dashboard(self, client):
        assert client.get("/treasurer/dashboard").status_code == 302

    def test_treasurer_deposit(self, client):
        assert client.get("/treasurer/savings/deposit").status_code == 302

    def test_member_dashboard(self, client):
        assert client.get("/member/dashboard").status_code == 302

    def test_member_savings(self, client):
        assert client.get("/member/savings").status_code == 302

    def test_member_repayments(self, client):
        assert client.get("/member/repayments").status_code == 302

    def test_secretary_dashboard(self, client):
        assert client.get("/secretary/dashboard").status_code == 302

    def test_publicity_dashboard(self, client):
        assert client.get("/publicity/dashboard").status_code == 302

    def test_publicity_announcements(self, client):
        assert client.get("/publicity/announcements").status_code == 302


# ============================================================
# 404 HANDLING
# ============================================================
def test_404(client):
    assert client.get("/this-does-not-exist-xyz").status_code == 404