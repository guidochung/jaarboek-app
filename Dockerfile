# Jaarboek: Streamlit-app met headless Chromium voor de PDF's (Hugging Face Space, Docker SDK)
FROM python:3.12-slim

# Chromium maakt de PDF's; Liberation Sans heeft dezelfde letterbreedtes als Helvetica/Arial,
# zodat de opmaak gelijk blijft aan het origineel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation fontconfig \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face draait containers als gebruiker 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    JAARBOEK_CONTAINER=1 \
    JAARBOEK_CHROME=/usr/bin/chromium
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

EXPOSE 7860
# XSRF uit: Hugging Face toont de app in een iframe, anders weigert Streamlit uploads (403).
CMD ["streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0", \
     "--server.enableXsrfProtection=false", "--server.enableCORS=false", "--server.headless=true"]
