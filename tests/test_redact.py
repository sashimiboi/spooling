"""Tests for spooling.redact — secret scrubbing logic."""

import pytest
from spooling.redact import redact_text, redact_messages, redact_value, _key_is_sensitive


# ---------------------------------------------------------------------------
# Vendor-pattern tests
# ---------------------------------------------------------------------------

class TestVendorPatterns:
    def test_anthropic_key(self):
        text = "use sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890abcdefghij to auth"
        out, hits = redact_text(text)
        assert "[REDACTED:ANTHROPIC_KEY]" in out
        assert len(hits) == 1
        assert hits[0].label == "ANTHROPIC_KEY"
        assert "sk-ant-api03" not in out

    def test_openai_key(self):
        text = "key = sk-proj-abcdefghijklmnopqrstu"
        out, hits = redact_text(text)
        assert "[REDACTED:OPENAI_KEY]" in out
        assert "sk-proj-" not in out

    def test_anthropic_wins_over_openai(self):
        # sk-ant-... should be labelled ANTHROPIC_KEY, not OPENAI_KEY
        text = "sk-ant-sid03-aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890abcdefghij"
        out, hits = redact_text(text)
        labels = {h.label for h in hits}
        assert "ANTHROPIC_KEY" in labels
        assert "OPENAI_KEY" not in labels

    def test_github_token(self):
        text = "export GITHUB_TOKEN=ghp_" + "A" * 36
        out, hits = redact_text(text)
        assert "[REDACTED:GITHUB_TOKEN]" in out

    def test_github_pat(self):
        text = "github_pat_" + "A" * 82
        out, hits = redact_text(text)
        assert "[REDACTED:GITHUB_PAT]" in out

    def test_aws_access_key(self):
        text = "AWS Key: AKIAIOSFODNN7EXAMPLE"
        out, hits = redact_text(text)
        assert "[REDACTED:AWS_ACCESS_KEY]" in out
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_aws_secret_key(self):
        text = "AWS_SECRET_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        out, hits = redact_text(text)
        assert any(h.label == "AWS_SECRET_KEY" for h in hits)
        assert "wJalrXUtnFEMI" not in out

    def test_google_api_key(self):
        text = "key=AIza" + "A" * 35
        out, hits = redact_text(text)
        assert "[REDACTED:GOOGLE_API_KEY]" in out

    def test_slack_token(self):
        text = "token: xoxb-12345678-abcdefghij"
        out, hits = redact_text(text)
        assert "[REDACTED:SLACK_TOKEN]" in out

    def test_stripe_key(self):
        text = "sk_live_" + "A" * 24
        out, hits = redact_text(text)
        assert "[REDACTED:STRIPE_KEY]" in out

    def test_jwt(self):
        # Minimal JWT-shaped token
        header = "eyJhbGciOiJIUzI1NiJ9"
        payload = "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        sig = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        text = f"token: {header}.{payload}.{sig}"
        out, hits = redact_text(text)
        assert "[REDACTED:JWT]" in out

    def test_pem_private_key(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Z3VS5JJcds3xHn/ygWep4\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out, hits = redact_text(pem)
        assert "[REDACTED:PRIVATE_KEY]" in out
        assert "MIIEow" not in out

    def test_no_false_positive_plain_text(self):
        text = "Hello world, this is a normal sentence with no secrets."
        out, hits = redact_text(text)
        assert out == text
        assert hits == []

    def test_no_false_positive_uuid(self):
        text = "session_id=550e8400-e29b-41d4-a716-446655440000"
        out, hits = redact_text(text)
        assert hits == []
        assert out == text

    def test_empty_string(self):
        out, hits = redact_text("")
        assert out == ""
        assert hits == []

    def test_multiple_secrets_same_text(self):
        sk = "sk-proj-" + "B" * 24
        gh = "ghp_" + "C" * 36
        text = f"openai={sk} github={gh}"
        out, hits = redact_text(text)
        assert sk not in out
        assert gh not in out
        assert len(hits) == 2


# ---------------------------------------------------------------------------
# ENV-var heuristic tests
# ---------------------------------------------------------------------------

class TestEnvVarHeuristic:
    def test_sensitive_env_var_redacted(self):
        text = "DATABASE_PASSWORD=s3cr3t_password_here"
        out, hits = redact_text(text)
        assert "s3cr3t_password_here" not in out
        assert any("ENV:" in h.label for h in hits)

    def test_api_key_env_var_redacted(self):
        text = "MY_API_KEY=abcdefg12345678"
        out, hits = redact_text(text)
        assert "abcdefg12345678" not in out

    def test_token_env_var_redacted(self):
        text = "SNOWFLAKE_TOKEN=very_long_token_value_here"
        out, hits = redact_text(text)
        assert "very_long_token_value_here" not in out

    def test_benign_env_var_not_redacted(self):
        text = "SNOWFLAKE_DATABASE=BACKYARD"
        out, hits = redact_text(text)
        assert out == text
        assert hits == []

    def test_host_env_var_not_redacted(self):
        text = "DB_HOST=my-database.example.com"
        out, hits = redact_text(text)
        assert out == text
        assert hits == []

    def test_port_env_var_not_redacted(self):
        text = "DB_PORT=5432"
        out, hits = redact_text(text)
        assert out == text
        assert hits == []

    def test_key_sensitivity_check(self):
        assert _key_is_sensitive("API_KEY") is True
        assert _key_is_sensitive("MY_SECRET") is True
        assert _key_is_sensitive("AUTH_TOKEN") is True
        assert _key_is_sensitive("DB_HOST") is False
        assert _key_is_sensitive("DB_PORT") is False
        assert _key_is_sensitive("SNOWFLAKE_DATABASE") is False
        assert _key_is_sensitive("APP_NAME") is False


# ---------------------------------------------------------------------------
# redact_messages helper
# ---------------------------------------------------------------------------

class TestRedactMessages:
    def test_redacts_content_in_place(self):
        sk = "sk-proj-" + "X" * 24
        messages = [
            {"role": "user", "content": f"my key is {sk}"},
            {"role": "assistant", "content": "noted"},
        ]
        result, total = redact_messages(messages)
        assert sk not in result[0]["content"]
        assert result[1]["content"] == "noted"
        assert total == 1

    def test_skips_non_string_content(self):
        messages = [{"role": "user", "content": None}]
        result, total = redact_messages(messages)
        assert total == 0

    def test_empty_list(self):
        result, total = redact_messages([])
        assert total == 0


# ---------------------------------------------------------------------------
# redact_value (recursive)
# ---------------------------------------------------------------------------

class TestRedactValue:
    def test_string(self):
        sk = "sk-proj-" + "Y" * 24
        new, n = redact_value(f"token: {sk}")
        assert sk not in new
        assert n == 1

    def test_list(self):
        sk = "sk-proj-" + "Z" * 24
        new, n = redact_value([f"key={sk}", "safe"])
        assert sk not in new[0]
        assert new[1] == "safe"
        assert n == 1

    def test_dict(self):
        sk = "sk-proj-" + "W" * 24
        new, n = redact_value({"key": f"val={sk}", "other": 42})
        assert sk not in new["key"]
        assert new["other"] == 42
        assert n == 1

    def test_integer_passthrough(self):
        new, n = redact_value(12345)
        assert new == 12345
        assert n == 0
