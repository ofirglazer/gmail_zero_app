"""
Presentation layer for gmail_zero_app.

Contains the Flask application factory, route blueprints, Jinja2 templates,
and static assets.  Routes are thin — they delegate all logic to application
services injected at app-factory time.

Sub-packages:
    routes/  — Flask Blueprint modules, one per workflow section

Implemented in Steps 6-8.
"""
