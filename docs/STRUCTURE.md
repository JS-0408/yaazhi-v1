# Yaazhi Documentation Structure

This directory contains all architectural decisions, design documents, and planning materials.

## Directory Overview

### `architecture/`
High-level system design and component interactions
- **Multi-Agent Orchestration**: LangGraph workflow and agent swarm design
- **Memory System**: ChromaDB + pgvector architecture
- **Voice Pipeline**: STT/TTS with Bhashini integration
- **API Design**: FastAPI routing and middleware architecture

### `identity/`
Identity and authentication architecture
- User session management
- Token validation schemes
- Multi-user support planning
- Permission model design

### `permissions/`
Authorization and access control
- Role-based access control (RBAC)
- Resource ownership model
- API endpoint security matrix
- Data isolation strategies

### `decisions/`
Architecture Decision Records (ADRs)
- Decision rationale and trade-offs
- Alternative approaches considered
- Implementation notes
- Revision history

### `blueprints/`
Detailed technical specifications
- API contract specifications
- Database schemas
- Message flow diagrams
- Integration points

### `audits/`
Security and performance audits
- Dependency vulnerability reports
- Type-checking audit results
- Performance benchmarks
- Security assessment findings

### `roadmap/`
Product planning and vision
- Version milestones (v1.0, v2.0, v3.0)
- Feature specifications
- Timeline estimates
- Priority rankings

## Contributing Documentation

1. **Architecture Changes**: Create an ADR in `decisions/`
2. **Design Discussions**: Use `blueprints/` for detailed specs
3. **Security Reviews**: Document findings in `audits/`
4. **Planning**: Update `roadmap/` with approved features

See [CONTRIBUTING.md](../CONTRIBUTING.md) for detailed guidelines.
