"""Tests for redact_sensitive() PII/secret scrubbing."""

from triagepilot.redaction import redact_sensitive


class TestEmails:
    def test_redacts_email(self):
        out = redact_sensitive("Crash reported by jane.doe@example.com during build")
        assert "jane.doe@example.com" not in out
        assert "[REDACTED_EMAIL]" in out


class TestPrivateKeys:
    def test_redacts_pem_block(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA1234567890abcdef\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact_sensitive(f"env dump:\n{pem}\nend")
        assert "MIIEpAIBAAKCAQEA1234567890abcdef" not in out
        assert "[REDACTED_PRIVATE_KEY]" in out


class TestKnownTokenFormats:
    def test_redacts_aws_access_key(self):
        out = redact_sensitive("AWS_ACCESS_KEY_ID is AKIAABCDEFGHIJKLMNOP in the crash env")
        assert "AKIAABCDEFGHIJKLMNOP" not in out
        assert "[REDACTED_TOKEN]" in out

    def test_redacts_openai_style_key(self):
        out = redact_sensitive("key=sk-abcdefghijklmnopqrstuvwx1234")
        assert "sk-abcdefghijklmnopqrstuvwx1234" not in out

    def test_redacts_github_token(self):
        out = redact_sensitive("token: ghp_" + "a" * 36)
        assert "ghp_" + "a" * 36 not in out

    def test_redacts_slack_token(self):
        out = redact_sensitive("SLACK_TOKEN=xoxb-1234567890-abcdefghij")
        assert "xoxb-1234567890-abcdefghij" not in out

    def test_redacts_bearer_header(self):
        out = redact_sensitive("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
        assert "[REDACTED_TOKEN]" in out


class TestSecretAssignments:
    def test_redacts_password_env_var(self):
        out = redact_sensitive("DB_PASSWORD=hunter2\nOTHER=fine")
        assert "hunter2" not in out
        assert "DB_PASSWORD=[REDACTED]" in out
        assert "OTHER=fine" in out

    def test_redacts_api_key_colon_form(self):
        out = redact_sensitive("api_key: abc123XYZ")
        assert "abc123XYZ" not in out

    def test_preserves_unrelated_assignments(self):
        out = redact_sensitive("BUILD_NUMBER=482\nRETRY_COUNT=3")
        assert "BUILD_NUMBER=482" in out
        assert "RETRY_COUNT=3" in out


class TestHomeDirUsernames:
    def test_redacts_unix_home_dir(self):
        out = redact_sensitive("Faulting file: /Users/johndoe/project/src/main.cpp:42")
        assert "johndoe" not in out
        assert "/Users/[USER]/project/src/main.cpp:42" in out

    def test_redacts_linux_home_dir(self):
        out = redact_sensitive("at /home/alice/repo/foo.c:10")
        assert "alice" not in out
        assert "/home/[USER]/repo/foo.c:10" in out

    def test_redacts_windows_home_dir(self):
        out = redact_sensitive(r"C:\Users\bob\source\repos\app\main.cpp(10)")
        assert "bob" not in out
        assert "[USER]" in out


class TestPassthrough:
    def test_empty_string(self):
        assert redact_sensitive("") == ""

    def test_ordinary_stack_trace_untouched(self):
        text = "#0  0x00007f in crash_handler () at main.cpp:42\n#1  0x00007f in main ()"
        assert redact_sensitive(text) == text
