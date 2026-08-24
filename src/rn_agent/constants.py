"""Every fixed name, path and threshold used by the agent."""

from __future__ import annotations

from typing import Final

APP_NAME: Final = "rn-agent"
APP_TITLE: Final = "React Native Agent"
APP_VERSION: Final = "0.1.0"

# --- environment overrides -------------------------------------------------
ENV_HOME: Final = "RN_AGENT_HOME"
ENV_PROJECT: Final = "RN_AGENT_PROJECT"
ENV_NO_COLOR: Final = "NO_COLOR"
ENV_KEYCHAIN: Final = "RN_AGENT_KEYCHAIN"

# --- project-local state ---------------------------------------------------
AGENT_DIR: Final = ".rn-agent"
CONFIG_FILE: Final = "config.yaml"
PROJECT_CONTEXT_FILE: Final = "project-context.json"
ARCHITECTURE_FILE: Final = "architecture.yaml"
RULES_FILE: Final = "rules.yaml"
DEPENDENCIES_FILE: Final = "dependencies.json"
MIGRATION_HISTORY_FILE: Final = "migration-history.json"
DECISIONS_FILE: Final = "decisions.md"
KNOWLEDGE_DIR: Final = "knowledge"
CACHE_DIR: Final = "cache"
LOGS_DIR: Final = "logs"
BACKUP_DIR: Final = "backups"
KNOWLEDGE_DB: Final = "knowledge.db"
IGNORE_FILE: Final = ".rn-agentignore"

# --- user-level state (never inside the project) ---------------------------
USER_CONFIG_DIR: Final = ".config/rn-agent"
USER_CONFIG_FILE: Final = "config.yaml"
# The credential index records *which* providers have a stored key and where it
# lives. It never holds the key itself.
USER_CREDENTIALS_INDEX: Final = "credentials.json"
# Only used by the labelled file fallback when no OS keychain is reachable.
USER_SECRETS_FILE: Final = "credentials.enc.json"
KEYCHAIN_SERVICE: Final = "rn-agent"

# --- React Native project markers -----------------------------------------
PACKAGE_JSON: Final = "package.json"
ANDROID_DIR: Final = "android"
IOS_DIR: Final = "ios"
RN_PACKAGE: Final = "react-native"
REACT_PACKAGE: Final = "react"
EXPO_PACKAGE: Final = "expo"

PROJECT_MARKER_FILES: Final[tuple[str, ...]] = (
    "package.json",
    "metro.config.js",
    "metro.config.ts",
    "metro.config.cjs",
    "babel.config.js",
    "babel.config.ts",
    "babel.config.cjs",
    "tsconfig.json",
    "app.json",
    "react-native.config.js",
    "react-native.config.ts",
    "jest.config.js",
    "jest.config.ts",
    ".eslintrc.js",
    ".eslintrc.json",
    "eslint.config.js",
    "eslint.config.mjs",
    ".watchmanconfig",
    "Gemfile",
    "index.js",
    "index.ts",
    "index.tsx",
    "App.tsx",
    "App.js",
)

LOCKFILES: Final[dict[str, str]] = {
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "bun.lockb": "bun",
    "bun.lock": "bun",
}

# --- limits ----------------------------------------------------------------
MAX_SCAN_DEPTH: Final = 6
MAX_SOURCE_FILES: Final = 20_000
MAX_FILE_READ_BYTES: Final = 2 * 1024 * 1024
DEFAULT_COMMAND_TIMEOUT: Final = 120.0
LOG_MAX_BYTES: Final = 4 * 1024 * 1024
LOG_BACKUP_COUNT: Final = 3

# Directories never walked when building the source-file inventory.
SOURCE_SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        "build",
        "dist",
        "coverage",
        "Pods",
        "DerivedData",
        ".gradle",
        ".idea",
        ".vscode",
        ".expo",
        ".next",
        ".yarn",
        ".pnpm-store",
        "vendor",
        "__pycache__",
        ".rn-agent",
        ".cxx",
        ".husky",
        "ios/build",
        "android/build",
    }
)

SOURCE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
)

# --- secrets: never read into AI context, always redacted in logs ----------
SECRET_FILE_PATTERNS: Final[tuple[str, ...]] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.p8",
    "*.p12",
    "*.mobileprovision",
    "*.keystore",
    "*.jks",
    "*.cer",
    "*.certSigningRequest",
    "id_rsa*",
    "*.key",
    "google-services.json",
    "GoogleService-Info.plist",
    "local.properties",
    "keystore.properties",
    "secrets.*",
    "*.secret",
    "sentry.properties",
    "fastlane/.env*",
)

SECRET_VALUE_PATTERNS: Final[tuple[str, ...]] = (
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"sk-ant-[A-Za-z0-9_\-]{16,}",
    r"gh[pousr]_[A-Za-z0-9]{16,}",
    r"AIza[0-9A-Za-z_\-]{20,}",
    r"AKIA[0-9A-Z]{12,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"(?i)(api[_-]?key|secret|password|passwd|token|bearer)[\"'\s:=]+[A-Za-z0-9_\-./+]{12,}",
)

REDACTED: Final = "[redacted]"

# --- health scoring --------------------------------------------------------
# Every lost point maps to a listed check, so the score is explainable.
# Weights make one build-breaking issue clearly visible without collapsing the
# whole score to zero on a project that has several.
SEVERITY_PENALTY: Final[dict[str, int]] = {
    "critical": 10,
    "high": 5,
    "medium": 2,
    "low": 1,
    "info": 0,
}
HEALTH_SCORE_MAX: Final = 100
