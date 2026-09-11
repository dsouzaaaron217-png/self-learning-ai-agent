import os
from flask import Flask, render_template
from app.config import OFFLINE_STRICT_MODE
from app.database import init_db
from app.models import MemoryModel, MemoryDecisionLogModel
from app.memory.pipeline import MemoryPipeline
from app.routes import register_blueprints

def create_app() -> Flask:
    """Application factory for Cognito Offline Agent."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static"
    )

    # Initialize SQLite database and tables
    init_db()

    # Seed baseline starter memories if database is fresh
    _seed_initial_memories()

    # Initialize Vector index
    MemoryPipeline.initialize_default_vector_index()

    # Register API blueprints
    register_blueprints(app)

    @app.route("/")
    def index():
        return render_template("index.html")

    return app

def _seed_initial_memories():
    """Seeds baseline productivity habits and decision logs if brand new."""
    existing = MemoryModel.list_all()
    if not existing:
        m1_id = MemoryModel.create(
            category="habit",
            content="User prefers deep work and development tasks before 12:00 PM.",
            confidence_weight=0.85,
            source_context="Initial productivity template",
            status="active"
        )
        MemoryDecisionLogModel.log(
            memory_id=m1_id,
            action="ADD",
            reasoning="Initialized default deep work scheduling preference.",
            triggered_by="system_init"
        )

        m2_id = MemoryModel.create(
            category="preference",
            content="Production bugs and system outages are marked Urgent priority.",
            confidence_weight=0.95,
            source_context="Initial productivity template",
            status="active"
        )
        MemoryDecisionLogModel.log(
            memory_id=m2_id,
            action="ADD",
            reasoning="Standard priority rule for production issues.",
            triggered_by="system_init"
        )
