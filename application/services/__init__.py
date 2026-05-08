"""
Application services for gmail_zero_app.

Each service encapsulates one use-case group.  Services receive all
dependencies (repositories, API client, settings) via constructor injection
— they never import concrete infrastructure classes directly.

Services implemented across Steps 2-8:
    LabelConfigService  — reads labels.toml, validates/creates Gmail labels
    SyncService         — full and incremental Gmail metadata sync
    LabelService        — safe label add/remove, routed through SafetyGuard
    AnalyticsService    — dashboard metric computations over local DB
    SearchService       — parameterised filtered message queries
"""
