"""Constants for the Health Auto Import integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "health_auto_import"
MANUFACTURER: Final = "HealthyApps"
MODEL: Final = "Health Auto Export (TCP/MCP server)"

# --- Connection ---------------------------------------------------------------
DEFAULT_PORT: Final = 9000
MIN_PORT: Final = 1
MAX_PORT: Final = 65535
CONNECT_TIMEOUT_S: Final = 10.0
READ_TIMEOUT_S: Final = 15.0
READ_TIMEOUT_HEAVY_S: Final = 30.0  # health_metrics can take 7-16s
READ_CHUNK_BYTES: Final = 65536
# Delay after each TCP request (inside lock) to let the iOS app recover.
INTER_REQUEST_DELAY_S: Final = 0.5
# Hard ceiling on a single JSON-RPC response to prevent OOM from a
# rogue/compromised server. 4 MiB accommodates a 30 s ECG with full voltage
# (15 360 floats ≈ 140 KiB JSON) plus workouts with metadata.
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_INCLUDE_ECG_VOLTAGE: Final = "include_ecg_voltage"
CONF_SELECTED_METRICS: Final = "selected_metrics"
CONF_INTERVALS: Final = "intervals"  # dict[domain -> seconds]

# --- JSON-RPC envelope (legacy v0.0.1) ----------------------------------------
RPC_METHOD_CALL_TOOL: Final = "callTool"
RPC_METHOD_CALL_TOOL_V1: Final = "tools/call"
RPC_METHOD_LIST_TOOLS: Final = "tools/list"
RPC_METHOD_LIST_TOOLS_LEGACY: Final = "listTools"

# Error codes observed on the live server.
RPC_ERR_METHOD_NOT_FOUND: Final = -32601
RPC_ERR_INVALID_PARAMS: Final = -32602  # also signals "unknown tool"

# --- Known tool names ---------------------------------------------------------
TOOL_ECG: Final = "ecg"
TOOL_WORKOUTS: Final = "workouts"
TOOL_HEART_NOTIFICATIONS: Final = "heart_notifications"
TOOL_HEALTH_METRICS: Final = "health_metrics"
TOOL_MEDICATIONS: Final = "medications"
TOOL_STATE_OF_MIND: Final = "state_of_mind"
TOOL_SYMPTOMS: Final = "symptoms"
TOOL_CYCLE_TRACKING: Final = "cycle_tracking"

KNOWN_TOOLS: Final = (
    TOOL_ECG,
    TOOL_WORKOUTS,
    TOOL_HEART_NOTIFICATIONS,
    TOOL_HEALTH_METRICS,
    TOOL_MEDICATIONS,
    TOOL_STATE_OF_MIND,
    TOOL_SYMPTOMS,
    TOOL_CYCLE_TRACKING,
)

# Tools that need initial backward crawl (sparse data).
SPARSE_TOOLS: Final = frozenset({
    TOOL_ECG,
    TOOL_HEART_NOTIFICATIONS,
    TOOL_MEDICATIONS,
    TOOL_STATE_OF_MIND,
    TOOL_SYMPTOMS,
    TOOL_CYCLE_TRACKING,
})

# Tools that are opt-in (sensitive data classes).
OPT_IN_TOOLS: Final = frozenset({
    TOOL_STATE_OF_MIND,
    TOOL_SYMPTOMS,
    TOOL_CYCLE_TRACKING,
})

# --- Polling intervals (seconds) -----------------------------------------------
# Tuned from 128 days of actual data (2026-01-16 to 2026-05-23):
#   health_metrics: daily rollups, some intraday → 30 min
#   workouts:       0.2/day (~weekly) → 10 min
#   ecg:            0.27/day (~weekly) → 30 min
#   heart_notif:    ~0/month → 1 hour
#   medications:    1.1/day (daily) → 30 min
# Total: ~300 TCP calls/day (was ~3,400 at old intervals).
INTERVAL_REACHABILITY_S: Final = 120
INTERVAL_HEALTH_METRICS_S: Final = 1800      # 30 min (was 10 min)
INTERVAL_ECG_S: Final = 1800                 # 30 min (was 5 min)
INTERVAL_WORKOUTS_S: Final = 600             # 10 min (was 1 min)
INTERVAL_HRN_S: Final = 3600                 # 1 hour (was 1 min)
INTERVAL_MEDICATIONS_S: Final = 1800         # 30 min (was 15 min)

TOOL_INTERVALS: Final[dict[str, int]] = {
    TOOL_ECG: INTERVAL_ECG_S,
    TOOL_WORKOUTS: INTERVAL_WORKOUTS_S,
    TOOL_HEART_NOTIFICATIONS: INTERVAL_HRN_S,
    TOOL_HEALTH_METRICS: INTERVAL_HEALTH_METRICS_S,
    TOOL_MEDICATIONS: INTERVAL_MEDICATIONS_S,
    TOOL_STATE_OF_MIND: INTERVAL_HEALTH_METRICS_S,
    TOOL_SYMPTOMS: INTERVAL_HEALTH_METRICS_S,
    TOOL_CYCLE_TRACKING: INTERVAL_HEALTH_METRICS_S,
}

# Floor and ceiling the adaptive scheduler is allowed to choose.
ADAPTIVE_MIN_INTERVAL_S: Final = 30
ADAPTIVE_MAX_INTERVAL_S: Final = 3600

# --- Watermark / catch-up (§3.9) ---------------------------------------------
# Overlap window added before watermark to catch late HealthKit syncs.
OVERLAP_SPARSE_S: Final = 5 * 60         # 5 min for sparse tools
OVERLAP_DENSE_S: Final = 30 * 60         # 30 min for health_metrics
# Maximum catch-up window after an outage.
MAX_CATCH_UP_SPARSE_S: Final = 14 * 86400   # 14 days
MAX_CATCH_UP_HEALTH_METRICS_S: Final = 7 * 86400   # 7 days
MAX_CATCH_UP_WORKOUTS_S: Final = 30 * 86400  # 30 days
# Initial backward crawl.
CRAWL_STEP_DAYS: Final = 30
CRAWL_FLOOR_DAYS: Final = 365 * 2  # 2 years
# Dedup LRU size per tool.
DEDUP_LRU_SIZE: Final = 500
# Seed window for dense tools (first load).
SEED_WINDOW_DAYS: Final = 7

# --- Security -----------------------------------------------------------------
# Maximum length for server-returned strings used in entity IDs / slugs.
MAX_SLUG_INPUT_LEN: Final = 128
# Allowed characters in hostnames (basic validation — not full RFC 952).
HOSTNAME_PATTERN: Final = r"^[a-zA-Z0-9._\-]+$"
# Maximum number of tools the server can advertise (prevent catalog-bomb).
MAX_TOOLS: Final = 50
# Maximum number of metrics from one health_metrics call.
MAX_METRICS: Final = 200
# Maximum number of records per tool response (prevents memory exhaustion
# from a rogue server claiming millions of ECG records).
MAX_RECORDS_PER_RESPONSE: Final = 10_000

# --- Default Sickbay metric set -----------------------------------------------
DEFAULT_METRICS: Final = (
    "heart_rate",
    "resting_heart_rate",
    "walking_heart_rate_average",
    "heart_rate_variability",
    "blood_oxygen_saturation",
    "blood_pressure",
    "body_temperature",
    "respiratory_rate",
    "blood_glucose",
    "step_count",
    "active_energy",
    "apple_exercise_time",
    "apple_stand_hour",
    "flights_climbed",
    "weight_body_mass",
    "body_fat_percentage",
    "body_mass_index",
    "lean_body_mass",
    "height",
    "vo2_max",
    "cardio_recovery",
    "sleep_analysis",
    "apple_sleeping_wrist_temperature",
    "mindful_minutes",
    "atrial_fibrillation_burden",
    "number_of_times_fallen",
)

# Full HAE metric ID list (snake_case). See 02-hae-tcp-protocol.md.
ALL_METRIC_IDS: Final = (
    "active_energy", "alcohol_consumption", "apple_exercise_time", "apple_move_time",
    "apple_sleeping_wrist_temperature", "apple_stand_hour", "apple_stand_time",
    "atrial_fibrillation_burden", "basal_body_temperature", "basal_energy_burned",
    "biotin", "blood_alcohol_content", "blood_glucose", "blood_oxygen_saturation",
    "blood_pressure", "body_fat_percentage", "body_mass_index", "body_temperature",
    "breathing_disturbances", "caffeine", "calcium", "carbohydrates",
    "cardio_recovery", "chloride", "cholesterol", "chromium", "copper",
    "cycling_cadence", "cycling_distance", "cycling_functional_threshold_power",
    "cycling_power", "cycling_speed", "dietary_energy", "dietary_sugar",
    "dietary_water", "distance_downhill_snow_sports", "electrodermal_activity",
    "environmental_audio_exposure", "fiber", "flights_climbed", "folate",
    "forced_expiratory_volume_1", "forced_vital_capacity", "handwashing",
    "headphone_audio_exposure", "heart_rate", "heart_rate_variability", "height",
    "inhaler_usage", "insulin_delivery", "iodine", "iron", "lean_body_mass",
    "magnesium", "manganese", "mindful_minutes", "molybdenum",
    "monounsaturated_fat", "niacin", "number_of_times_fallen", "pantothenic_acid",
    "peak_expiratory_flow_rate", "peripheral_perfusion_index", "phosphorus",
    "physical_effort", "polyunsaturated_fat", "potassium", "protein", "push_count",
    "respiratory_rate", "resting_heart_rate", "riboflavin",
    "running_ground_contact_time", "running_power", "running_speed",
    "running_stride_length", "running_vertical_oscillation", "saturated_fat",
    "selenium", "sexual_activity", "six_minute_walking_test_distance",
    "sleep_analysis", "sodium", "stair_speed_down", "stair_speed_up", "step_count",
    "swimming_distance", "swimming_stroke_count", "thiamin", "time_in_daylight",
    "toothbrushing", "total_fat", "underwater_depth", "underwater_temperature",
    "uv_exposure", "vitamin_a", "vitamin_b12", "vitamin_b6", "vitamin_c",
    "vitamin_d", "vitamin_e", "vitamin_k", "vo2_max", "waist_circumference",
    "walking_asymmetry_percentage", "walking_double_support_percentage",
    "walking_heart_rate_average", "walking_running_distance", "walking_speed",
    "walking_step_length", "weight_body_mass", "wheelchair_distance", "zinc",
)

# --- Date format used in HAE TCP arguments ------------------------------------
HAE_TS_FORMAT: Final = "%Y-%m-%d %H:%M:%S %z"
