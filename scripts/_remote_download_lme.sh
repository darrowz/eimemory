set -e
cd /opt/eimemory/data
rm -f longmemeval_s_cleaned.json
# HuggingFace LFS requires specific headers
curl -L -H "User-Agent: Mozilla/5.0" \
  "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json" \
  -o longmemeval_s_cleaned.json 2>&1 | tail -5
ls -lah longmemeval_s_cleaned.json
head -c 200 longmemeval_s_cleaned.json
echo ""
echo "--- DONE ---"
