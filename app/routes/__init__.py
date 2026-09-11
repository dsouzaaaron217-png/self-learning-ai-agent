from flask import Flask
from app.routes.api_tasks import api_tasks_bp
from app.routes.api_notes import api_notes_bp
from app.routes.api_memory import api_memory_bp
from app.routes.api_chat import api_chat_bp
from app.routes.api_backup import api_backup_bp

def register_blueprints(app: Flask):
    app.register_blueprint(api_tasks_bp)
    app.register_blueprint(api_notes_bp)
    app.register_blueprint(api_memory_bp)
    app.register_blueprint(api_chat_bp)
    app.register_blueprint(api_backup_bp)
