from .recordings import recordings_bp
from .dashboard import dashboard_bp
from .settings import settings_bp
from .accounts import accounts_bp
from .guide import guide_bp
from .channel_tests import channel_tests_bp
from .channels import channels_bp
from .system import system_bp
from .jobs import jobs_bp
from .logs import logs_bp
from .alerts import alerts_bp
from .profiles import profiles_bp
from .health_check_profiles import health_check_profiles_bp
from .tags import tags_bp
from .channel_groups import channel_groups_bp
from .channel_search import channel_search_bp
from .channel_hide_rules import channel_hide_rules_bp
from .auth import auth_bp
from .ha import ha_bp


def register_blueprints(app):
    app.register_blueprint(recordings_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(guide_bp)
    app.register_blueprint(channel_tests_bp)
    app.register_blueprint(channels_bp)
    app.register_blueprint(system_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(profiles_bp)
    app.register_blueprint(health_check_profiles_bp)
    app.register_blueprint(tags_bp)
    app.register_blueprint(channel_groups_bp)
    app.register_blueprint(channel_search_bp)
    app.register_blueprint(channel_hide_rules_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(ha_bp)
