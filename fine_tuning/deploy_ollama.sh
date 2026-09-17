#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  NEXUS AI · fine_tuning/deploy_ollama.sh
#  Register the fine-tuned GGUF model with local Ollama
#  Run after downloading the GGUF file from Colab
# ─────────────────────────────────────────────────────────────
set -e

GGUF_PATH="${1:-./fine_tuning/nexus-finetuned-Q4_K_M.gguf}"
MODEL_NAME="nexus-it"

if [ ! -f "$GGUF_PATH" ]; then
  echo "❌ GGUF file not found: $GGUF_PATH"
  echo "Usage: ./fine_tuning/deploy_ollama.sh /path/to/model.gguf"
  exit 1
fi

echo "📦 Creating Modelfile..."
cat > /tmp/Modelfile << EOF
FROM $GGUF_PATH

SYSTEM """You are NEXUS AI, an expert IT Service Management assistant trained on
real IT incident resolutions. Provide precise root cause analysis and step-by-step
resolution plans. Always include verification steps and prevention advice."""

PARAMETER temperature 0.1
PARAMETER top_p 0.9
PARAMETER repeat_penalty 1.1
PARAMETER num_ctx 2048
EOF

echo "🚀 Registering model with Ollama as '$MODEL_NAME'..."
ollama create "$MODEL_NAME" -f /tmp/Modelfile

echo "✅ Model registered. Testing..."
ollama run "$MODEL_NAME" "What causes PostgreSQL connection pool exhaustion?"

echo ""
echo "✅ Fine-tuned model '$MODEL_NAME' is live!"
echo "Update OLLAMA_MODEL in your .env to use it:"
echo "  EMBEDDING_MODEL_OLLAMA=nomic-embed-text"
echo "  LLM_MODEL=nexus-it   # for the fine-tuned model in agents"
