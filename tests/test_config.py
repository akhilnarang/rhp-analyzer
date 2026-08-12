import os
from unittest import TestCase
from unittest.mock import patch

from pydantic import SecretStr

from rhp_analyzer.config import Settings


class SettingsTests(TestCase):
    def test_optional_settings_have_openai_defaults(self) -> None:
        settings = Settings(openai_api_key=SecretStr("test-key"), _env_file=None)
        self.assertEqual(settings.openai_base_url, "https://api.openai.com/v1")
        self.assertEqual(settings.rhp_default_model, "gpt-5.6-terra")
        self.assertEqual(settings.rhp_default_retries, 2)
        self.assertEqual(settings.rhp_max_pdf_bytes, 50_000_000)
        self.assertEqual(settings.rhp_section_concurrency, 4)
        self.assertEqual(settings.rhp_job_concurrency, 1)
        self.assertEqual(
            settings.allowed_pdf_hosts(),
            {"www.bseindia.com", "bseindia.com"},
        )
        self.assertEqual(settings.api_tokens(), set())

    def test_api_tokens_are_parsed_and_masked(self) -> None:
        settings = Settings(
            openai_api_key=SecretStr("test-key"),
            rhp_api_tokens=SecretStr(" token-one,token-two "),
            _env_file=None,
        )
        self.assertEqual(settings.api_tokens(), {"token-one", "token-two"})
        self.assertNotIn("token-one", repr(settings))

    def test_secret_is_masked(self) -> None:
        settings = Settings(
            openai_api_key=SecretStr("test-secret"),
            openai_base_url="https://example.invalid/v1",
            _env_file=None,
        )
        self.assertEqual(settings.require_openai_api_key(), "test-secret")
        self.assertNotIn("test-secret", repr(settings))

    def test_missing_key_has_actionable_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
        with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
            settings.require_openai_api_key()
