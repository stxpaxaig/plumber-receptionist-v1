"""
Tests for Telnyx integration: TeXML routing and provisioning helpers.
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from telnyx_provisioner import (
    ensure_texml_app,
    provision_phone_number,
    provision_telnyx,
    TELNYX_APP_NAME,
)
from app import create_app


class TestTeXMLRoute:
    """Test /webhooks/telnyx TeXML response."""

    def test_telnyx_webhook_missing_project_id(self):
        """Without OPENAI_PROJECT_ID, should return 503."""
        app = create_app({"OPENAI_PROJECT_ID": ""})
        client = app.test_client()
        response = client.post("/webhooks/telnyx")
        assert response.status_code == 503
        assert "not_configured" in response.get_json().get("error", "")

    def test_telnyx_webhook_returns_texml(self):
        """With OPENAI_PROJECT_ID, should return valid TeXML."""
        app = create_app({"OPENAI_PROJECT_ID": "proj_test123"})
        client = app.test_client()
        response = client.post("/webhooks/telnyx")
        assert response.status_code == 200
        assert response.content_type == "application/xml"
        body = response.get_data(as_text=True)
        assert "<Response>" in body
        assert "<Sip>sip:proj_test123@sip.api.openai.com;transport=tls</Sip>" in body

    def test_telnyx_webhook_xml_escaping(self):
        """TeXML should XML-escape project ID if it contains special chars."""
        app = create_app({"OPENAI_PROJECT_ID": "proj_with&chars<>"})
        client = app.test_client()
        response = client.post("/webhooks/telnyx")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # XML escaping: & -> &amp;, < -> &lt;, > -> &gt;
        assert "proj_with&amp;chars&lt;&gt;" in body

    def test_telnyx_webhook_get_method(self):
        """GET /webhooks/telnyx should also work (some clients test with GET)."""
        app = create_app({"OPENAI_PROJECT_ID": "proj_test123"})
        client = app.test_client()
        response = client.get("/webhooks/telnyx")
        assert response.status_code == 200
        assert response.content_type == "application/xml"


class TestTeXMLAppProvisioning:
    """Test ensure_texml_app idempotent logic."""

    @patch("telnyx_provisioner.requests.get")
    @patch("telnyx_provisioner.requests.post")
    def test_ensure_texml_app_creates_new(self, mock_post, mock_get):
        """Should create app if it doesn't exist."""
        mock_get.return_value = Mock(
            json=lambda: {"data": []},  # No apps exist
            raise_for_status=Mock(),
        )
        mock_post.return_value = Mock(
            json=lambda: {"data": {"id": "app_new123"}},
            raise_for_status=Mock(),
        )

        result = ensure_texml_app("test_api_key", "example.railway.app")

        assert result == "app_new123"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert payload["friendly_name"] == TELNYX_APP_NAME
        assert payload["active"] is True
        assert payload["voice_url"] == "https://example.railway.app/webhooks/telnyx"
        assert payload["voice_method"] == "post"

    @patch("telnyx_provisioner.requests.get")
    @patch("telnyx_provisioner.requests.patch")
    def test_ensure_texml_app_updates_existing(self, mock_patch, mock_get):
        """Should update existing app if voice_url differs."""
        existing_app = {
            "id": "app_old123",
            "friendly_name": TELNYX_APP_NAME,
            "active": False,  # Should be updated
            "voice_url": "https://old-url.railway.app/webhooks/telnyx",
            "voice_method": "post",
        }
        mock_get.return_value = Mock(
            json=lambda: {"data": [existing_app]},
            raise_for_status=Mock(),
        )
        mock_patch.return_value = Mock(
            json=lambda: {"data": existing_app},
            raise_for_status=Mock(),
        )

        result = ensure_texml_app("test_api_key", "new-url.railway.app")

        assert result == "app_old123"
        mock_patch.assert_called_once()
        call_args = mock_patch.call_args
        assert "new-url.railway.app" in call_args[1]["json"]["voice_url"]

    @patch("telnyx_provisioner.requests.get")
    def test_ensure_texml_app_already_correct(self, mock_get):
        """Should return existing app ID if already configured correctly."""
        existing_app = {
            "id": "app_correct123",
            "friendly_name": TELNYX_APP_NAME,
            "active": True,
            "voice_url": "https://example.railway.app/webhooks/telnyx",
            "voice_method": "post",
        }
        mock_get.return_value = Mock(
            json=lambda: {"data": [existing_app]},
            raise_for_status=Mock(),
        )

        result = ensure_texml_app("test_api_key", "example.railway.app")

        assert result == "app_correct123"
        # No PATCH should be called
        mock_get.assert_called_once()

    @patch("telnyx_provisioner.requests.get")
    def test_ensure_texml_app_missing_credentials(self, mock_get):
        """Should return None if credentials are missing."""
        result = ensure_texml_app("", "example.railway.app")
        assert result is None
        mock_get.assert_not_called()

    @patch("telnyx_provisioner.requests.get")
    def test_ensure_texml_app_api_error(self, mock_get):
        """Should return None if API call fails."""
        mock_get.side_effect = Exception("Network error")
        result = ensure_texml_app("test_api_key", "example.railway.app")
        assert result is None


class TestPhoneNumberProvisioning:
    """Test provision_phone_number idempotent logic."""

    @patch("telnyx_provisioner.requests.get")
    @patch("telnyx_provisioner.requests.patch")
    def test_provision_phone_number_routes_to_app(self, mock_patch, mock_get):
        """Should route phone number to app if not already routed."""
        number_record = {
            "id": "num_123",
            "phone_number": "+12098010772",
            "connection_id": None,  # Not yet routed
        }
        mock_get.return_value = Mock(
            json=lambda: {"data": [number_record]},
            raise_for_status=Mock(),
        )
        mock_patch.return_value = Mock(
            json=lambda: {"data": number_record},
            raise_for_status=Mock(),
        )

        result = provision_phone_number(
            "test_api_key", "+12098010772", "app_target123"
        )

        assert result is True
        mock_patch.assert_called_once()
        call_args = mock_patch.call_args
        assert call_args[1]["json"]["connection_id"] == "app_target123"

    @patch("telnyx_provisioner.requests.get")
    def test_provision_phone_number_already_routed(self, mock_get):
        """Should return True if phone number already routed to the app."""
        number_record = {
            "id": "num_123",
            "phone_number": "+12098010772",
            "connection_id": "app_target123",  # Already routed
        }
        mock_get.return_value = Mock(
            json=lambda: {"data": [number_record]},
            raise_for_status=Mock(),
        )

        result = provision_phone_number(
            "test_api_key", "+12098010772", "app_target123"
        )

        assert result is True

    @patch("telnyx_provisioner.requests.get")
    def test_provision_phone_number_not_found(self, mock_get):
        """Should return False if phone number doesn't exist."""
        mock_get.return_value = Mock(
            json=lambda: {"data": []},  # No numbers found
            raise_for_status=Mock(),
        )

        result = provision_phone_number(
            "test_api_key", "+12098010772", "app_target123"
        )

        assert result is False

    @patch("telnyx_provisioner.requests.get")
    def test_provision_phone_number_missing_credentials(self, mock_get):
        """Should return False if credentials are missing."""
        result = provision_phone_number("", "+12098010772", "app_target123")
        assert result is False
        mock_get.assert_not_called()

    @patch("telnyx_provisioner.requests.get")
    def test_provision_phone_number_api_error(self, mock_get):
        """Should return False if API call fails."""
        mock_get.side_effect = Exception("Network error")
        result = provision_phone_number(
            "test_api_key", "+12098010772", "app_target123"
        )
        assert result is False


class TestProvisioningOrchestration:
    """Test provision_telnyx full orchestration."""

    @patch("telnyx_provisioner.ensure_texml_app")
    @patch("telnyx_provisioner.provision_phone_number")
    def test_provision_telnyx_success(self, mock_phone, mock_app):
        """Should return True if both steps succeed."""
        mock_app.return_value = "app_123"
        mock_phone.return_value = True

        result = provision_telnyx(
            api_key="test_key",
            phone_number="+12098010772",
            public_domain="example.railway.app",
        )

        assert result is True
        mock_app.assert_called_once_with("test_key", "example.railway.app")
        mock_phone.assert_called_once_with("test_key", "+12098010772", "app_123")

    @patch("telnyx_provisioner.ensure_texml_app")
    def test_provision_telnyx_app_fails(self, mock_app):
        """Should return False if app provisioning fails."""
        mock_app.return_value = None

        result = provision_telnyx(
            api_key="test_key",
            phone_number="+12098010772",
            public_domain="example.railway.app",
        )

        assert result is False

    @patch("telnyx_provisioner.ensure_texml_app")
    @patch("telnyx_provisioner.provision_phone_number")
    def test_provision_telnyx_phone_fails(self, mock_phone, mock_app):
        """Should return False if phone provisioning fails."""
        mock_app.return_value = "app_123"
        mock_phone.return_value = False

        result = provision_telnyx(
            api_key="test_key",
            phone_number="+12098010772",
            public_domain="example.railway.app",
        )

        assert result is False

    def test_provision_telnyx_missing_credentials(self):
        """Should return False if credentials are missing, not raise."""
        result = provision_telnyx(
            api_key="",
            phone_number="",
            public_domain="",
        )
        assert result is False

    def test_provision_telnyx_from_env_missing(self, monkeypatch):
        """Should return False if env vars are missing."""
        monkeypatch.delenv("TELNYX_API_KEY", raising=False)
        monkeypatch.delenv("TELNYX_PHONE_NUMBER", raising=False)
        monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)

        result = provision_telnyx()

        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

