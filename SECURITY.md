# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |

## Reporting a Vulnerability

**Do NOT open a public issue for security vulnerabilities.**

If you discover a security vulnerability in TriagePilot, please report it responsibly:

1. Email **akash.goyal2790@gmail.com** with a description of the vulnerability
2. Include steps to reproduce, if possible
3. Allow up to **7 days** for an initial acknowledgment
4. We will work with you to understand the issue and provide a fix timeline within **30 days**

## Scope

TriagePilot spawns debugger subprocesses (CDB, GDB, LLDB) and processes crash dump files. The following areas are explicitly in scope for security reports:

- Command injection via debugger command inputs
- Path traversal in dump file or symbol path handling
- Unauthorized file access through debugger commands
- Denial of service via resource exhaustion (session limits, subprocess management)

## Acknowledgments

We appreciate responsible disclosure and will credit reporters in release notes (unless you prefer to remain anonymous).
