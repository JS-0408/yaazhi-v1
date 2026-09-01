# Security Policy

## Reporting Security Vulnerabilities

If you discover a security vulnerability in Yaazhi, **please do not open a public issue**. Instead:

1. **Email**: Send details to the maintainer (do not disclose publicly)
2. **Include**:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if available)

3. **Timeline**: We aim to respond within 48 hours

## Security Best Practices

### Secrets Management

- **Never commit secrets**: Use `config/.env` (already in `.gitignore`)
- **Use environment variables**: All credentials should be loaded from `.env`
- **Rotate credentials regularly**: Especially API keys and database passwords
- **Check `.gitignore`**: Verify sensitive files are listed before committing

### API Security

- **Authentication**: All endpoints require token validation (see `api/middleware.py`)
- **Rate Limiting**: Enabled by default to prevent abuse
- **Input Validation**: Use Pydantic models for all user input
- **Output Sanitization**: Strip sensitive data from error messages

### Code Security

- **Dependency Audits**: Run `pip-audit` regularly
  ```bash
  pip install pip-audit
  pip-audit
  ```
- **Type Safety**: Use `mypy --strict` to catch type-related bugs
- **SQL Injection**: Use parameterized queries (SQLAlchemy ORM by default)
- **XXE Prevention**: XML parsing handled safely via configured parsers

### Deployment Security

- **HTTPS Only**: Always use HTTPS in production
- **Cloudflare Tunnel**: Use authenticated tunnel (see `infra/docker-compose.yml`)
- **Database**: 
  - Run PostgreSQL in isolated VPC
  - Use strong passwords (30+ chars, special chars)
  - Enable SSL connections
- **VPS**:
  - Firewall rules: Close all ports except 80, 443, SSH
  - SSH key-only access (no passwords)
  - Regular OS updates

### Voice/Audio Security

- **Recording Consent**: Ensure user consent before recording audio
- **Encryption**: Audio streams should be encrypted in transit (HTTPS/WSS)
- **Data Retention**: Delete voice recordings after processing (see `config/settings.py`)

### Memory/Knowledge Base Security

- **Access Control**: Only authenticated users can ingest documents
- **Data Isolation**: Multi-user support should isolate memory per user
- **Sensitive Redaction**: PII in documents should be flagged/redacted

## Vulnerability Disclosure Timeline

- **Day 0**: Vulnerability reported
- **Day 1**: Confirm receipt and timeline
- **Day 7**: Working patch prepared
- **Day 14**: Patch released (coordinated with reporter if embargo requested)
- **Day 21**: Public disclosure and CVE (if applicable)

## Security Updates

- Subscribe to security notifications: Check the repository's "Security" tab
- **Critical patches**: Deploy within 24 hours
- **High patches**: Deploy within 1 week
- **Medium/Low patches**: Deploy within 1 month

## Compliance

Yaazhi does not handle:
- Payment information (no PCI-DSS required)
- Regulated health data (HIPAA/similar not required)
- Personal financial data (regulated by user's jurisdiction)

However:
- User conversation history is sensitive data — treat as PII
- Voice recordings may contain identifying information
- PDF uploads from users may contain confidential material

## Testing

Security testing includes:
- Dependency audit: `pip-audit` (runs in CI)
- Type checking: `mypy --strict` (runs in CI)
- Linting: `ruff` (runs in CI)

**Manual security review** should be done before major releases.

## Resources

- OWASP Top 10: https://owasp.org/www-project-top-ten/
- Python Security: https://python.readthedocs.io/en/latest/library/security_warnings.html
- FastAPI Security: https://fastapi.tiangolo.com/tutorial/security/

## Questions?

For security-related questions that don't involve vulnerabilities, you can:
- Open a discussion in the GitHub repository
- Email the maintainer privately

Thank you for helping keep Yaazhi secure! 🔒
