"""
Where things live, which ports they use, and every setting that differs from production.

The last part matters most. A rehearsal that quietly ran with shorter timers would produce numbers
somebody later mistakes for production timing, so every override is declared here once, with the
production value beside it, and the report prints this table verbatim.
"""

import os
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = TOOL_DIR.parent.parent

PROJECT = "havenz_rehearsal"

API_PORT = 5100
DB_PORT = 5434
SMTP_PORT = 1026
SINK_API_PORT = 8026
DASHBOARDS_PORT = 3100
DOOR_PORT = 3200
READER_CONTROL_PORT = 9100
AGENT_PAGE_PORT = 8199

# The harness talks to 127.0.0.1 itself: on Windows "localhost" is tried over IPv6 first and costs
# two seconds a call before it falls back. Browsers do not have that problem, so everything that is
# handed to a browser (the manifest, the apps' own configuration) says localhost, as the contract does.
API = f"http://127.0.0.1:{API_PORT}"
API_PUBLIC = f"http://localhost:{API_PORT}"
SINK = f"http://127.0.0.1:{SINK_API_PORT}"
READERS = f"http://127.0.0.1:{READER_CONTROL_PORT}"
AGENT_PAGE = f"http://127.0.0.1:{AGENT_PAGE_PORT}"
DASHBOARDS = f"http://localhost:{DASHBOARDS_PORT}"
DOOR = f"http://localhost:{DOOR_PORT}"

# The plant LAN inside the compose project.
READER_SUBNET = "10.107.0"
READER_FIRST_HOST = 11
READER_COUNT = 20
AGENT_LAN_ADDRESS = "10.107.0.2"

DB_PASSWORD = "rehearsal-only"          # the database never leaves 127.0.0.1:5434

CONTAINER = {
    "db": f"{PROJECT}-db-1",
    "api": f"{PROJECT}-api-1",
    "sink": f"{PROJECT}-sink-1",
    "readers": f"{PROJECT}-readers-1",
    "agent": f"{PROJECT}-agent-1",
}
UPLINK_NETWORK = f"{PROJECT}_uplink"


def workspace_root():
    """
    The folder that holds the sibling checkouts (HavenzBMS, the dashboards, the door app).
    Found by walking up from this tool, so the same code works from the main checkout and from a
    worktree; REHEARSAL_WORKSPACE overrides it.
    """
    env = os.environ.get("REHEARSAL_WORKSPACE")
    if env:
        return Path(env)
    for parent in TOOL_DIR.parents:
        if (parent / "HavenzBMS").is_dir() or (parent / "Havenz-docs").is_dir():
            return parent
    return REPO_DIR.parent


def output_root():
    env = os.environ.get("REHEARSAL_OUT")
    return Path(env) if env else workspace_root() / "output" / "plant-rehearsal"


def stack_dir():
    return output_root() / "_stack"


def default_paths():
    ws = workspace_root()

    def first(*candidates):
        for c in candidates:
            if c.is_dir():
                return c
        return candidates[-1]

    return {
        "bms": first(ws / "_wt" / "bms-preplant", ws / "HavenzBMS"),
        "dashboards": first(ws / "_wt" / "dash-3-20", ws / "Havenz-hub-dashboards"),
        "door": first(ws / "Havenz-door-access-control"),
    }


# ---------------------------------------------------------------------------------------------
# Settings that differ from production.
#
# (setting, rehearsal value, production value, why)
#
# "isolation" rows keep the rehearsal on this machine; they change where things go, not how the
# product behaves. "time" rows shorten a wait; anything measured across one of them is NOT a
# production timing and the report says so beside the number. "setup" rows are choices an
# administrator could make differently. Rate limits are deliberately NOT in this table: every one
# is left at its production value, and the harness paces itself to stay inside them.
# ---------------------------------------------------------------------------------------------
OVERRIDES = [
    # -- isolation --
    ("isolation", "ConnectionStrings__BMSConnection", "the rehearsal's own PostgreSQL (:5434)",
     "Cloud SQL", "Never the development or production database."),
    ("isolation", "Email__SmtpHost / SmtpPort / EnableSsl", "sink:1026, no TLS", "smtp.gmail.com:587, STARTTLS",
     "Mail is captured locally and delivered to nobody."),
    ("isolation", "DataProtection__KeyPath", "/keys (a volume)", "key ring in the documents bucket",
     "No cloud bucket in a rehearsal. The backend logs its 'keys are not durable' warning at start; expected here."),
    ("isolation", "GcpStorage (documents bucket)", "a local stand-in answering as storage.googleapis.com",
     "Google Cloud Storage",
     "Face photos must be stored somewhere or no face reaches a reader. The backend's storage client "
     "is unchanged; inside its container Google's storage and token host names resolve to the sink, "
     "trusted through a private certificate authority generated at start."),
    ("isolation", "Firebase (push)", "not configured", "Firebase Cloud Messaging",
     "Push goes nowhere. The in-app notification row is still written and is what the scenarios assert on."),
    ("isolation", "Webhooks__FaceEnrollmentUrl / AiServiceUrl", "the local sink", "the AI service",
     "The backend's outbound calls stay on this machine."),
    ("isolation", "Cors__AllowedOrigins", "adds http://localhost:3100 and :3200", "production origins only",
     "So the locally served wall and door apps may call the rehearsal API from a browser."),
    ("isolation", "Email__FrontendBaseUrl", "http://localhost:3000", "the production web app",
     "Links inside captured mail must not point at production."),

    # -- time --
    ("time", "Alerts__AgentOfflineAfterSeconds", "120", "300",
     "Site-agent-offline alert threshold. The code floors it at 90 s."),
    ("time", "Alerts__SweepIntervalSeconds", "30", "300",
     "How often silent sensors, leak timers and the offline agent are checked. 30 s is the code's floor."),
    ("time", "Alerts__DeliveryPollSeconds", "5", "30",
     "How often the delivery worker looks for due mail and push. Bounds how late a held page can leave."),
    ("time", "sensor reporting interval (per device)",
     "15 s for engines, meters and temperature (stale after 30 s); 30 s for leak and contact sensors "
     "(stale after 60 s, silent after 180 s)",
     "set per device; 900 s when unset (stale after 30 min, silent after 90 min)",
     "So a sensor going quiet can be watched in minutes, not hours."),
    ("time", "contractor job window", "minutes long", "hours or days",
     "The job's start and end are placed a minute or two apart so before / inside / after fit in one run."),

    # -- setup --
    ("setup", "company security policy: MFA", "encouraged", "required (the default for a new company)",
     "The harness signs in as staff and as a contractor without an authenticator app. With 'required', "
     "every one of those accounts must enrol an authenticator before any other call is allowed."),

]

# Environment for the API container. Kept next to the table above so the two cannot drift.
API_ENV = {
    "ASPNETCORE_ENVIRONMENT": "Production",
    "ASPNETCORE_URLS": "http://+:80",
    "ConnectionStrings__BMSConnection":
        f"Host=db;Port=5432;Database=havenzhub;Username=postgres;Password={DB_PASSWORD}",
    "JwtSettings__Issuer": "HavenzHub",
    "JwtSettings__Audience": "HavenzHubAPI",
    "DataProtection__KeyPath": "/keys",
    "Email__SmtpHost": "sink",
    "Email__SmtpPort": str(SMTP_PORT),
    "Email__EnableSsl": "false",
    "Email__FromEmail": "rehearsal@havenz.invalid",
    "Email__FrontendBaseUrl": "http://localhost:3000",
    "GcpStorage__BucketName": "havenz-rehearsal-documents",
    "GcpStorage__LogosBucketName": "havenz-rehearsal-logos",
    "GcpStorage__AvatarsBucketName": "havenz-rehearsal-avatars",
    "GcpStorage__CredentialsPath": "/gcs/sa.json",
    "GOOGLE_APPLICATION_CREDENTIALS": "/gcs/sa.json",
    "SSL_CERT_FILE": "/gcs/ca.pem",
    "Webhooks__FaceEnrollmentUrl": f"http://sink:{SINK_API_PORT}/hooks/face-enrollment",
    "Webhooks__FaceEnrollmentCallbackBaseUrl": "http://api",
    "Webhooks__AiServiceUrl": f"http://sink:{SINK_API_PORT}/hooks/document",
    "AmicoSettings__BackendBaseUrl": "http://api",
    # .NET merges configuration arrays by index; production lists five origins (0-4).
    "Cors__AllowedOrigins__5": f"http://localhost:{DASHBOARDS_PORT}",
    "Cors__AllowedOrigins__6": f"http://localhost:{DOOR_PORT}",
    "Cors__AllowedOrigins__7": f"http://127.0.0.1:{DASHBOARDS_PORT}",
    "Cors__AllowedOrigins__8": f"http://127.0.0.1:{DOOR_PORT}",
    "Alerts__AgentOfflineAfterSeconds": "120",
    "Alerts__SweepIntervalSeconds": "30",
    "Alerts__DeliveryPollSeconds": "5",
}

SENSOR_REPORTING_INTERVAL_SECONDS = 15          # engines, meters, temperature: a new measurement every poll
BINARY_REPORTING_INTERVAL_SECONDS = 30          # leak and contact sensors: on change, then a heartbeat every 40 s
ENGINE_RATED_KW = 2518
