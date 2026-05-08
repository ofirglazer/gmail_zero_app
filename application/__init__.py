"""
Application layer for gmail_zero_app.

Contains use-case services and DTOs.  This layer depends only on the domain
layer and on abstract interfaces (Protocols) for infrastructure — never on
concrete infrastructure implementations directly.

Sub-packages:
    services/  — Use-case services (SyncService, LabelService, etc.)
    dto/       — Data transfer objects crossing layer boundaries
"""
