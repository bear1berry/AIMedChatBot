# === AIMLAPI config ===

AIML_API_KEY = os.getenv("AIML_API_KEY", "")
AIML_API_URL = os.getenv("AIML_API_URL", "https://api.aimlapi.com/v1/chat/completions")

# Основные модели
AIML_MODEL_PRIMARY = os.getenv("AIML_MODEL_PRIMARY", "openai/gpt-4.1")
AIML_MODEL_FAST = os.getenv("AIML_MODEL_FAST", "openai/gpt-4.1-mini")
AIML_MODEL_GPT_OSS_120B = os.getenv("AIML_MODEL_GPT_OSS_120B", "openai/gpt-oss-120b")
AIML_MODEL_DEEPSEEK_REASONER = os.getenv("AIML_MODEL_DEEPSEEK_REASONER", "deepseek/deepseek-reasoner")
AIML_MODEL_DEEPSEEK_CHAT = os.getenv("AIML_MODEL_DEEPSEEK_CHAT", "deepseek/deepseek-chat")
