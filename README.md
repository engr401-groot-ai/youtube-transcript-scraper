# Run on Local
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

GOOGLE_APPLICATION_CREDENTIALS=" "
.venv/bin/python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000