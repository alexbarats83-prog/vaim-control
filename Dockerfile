FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=pip_ca \
    if [ -r /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --system --user-group vaim \
    && install -d -o vaim -g vaim /app/instance
COPY vaim/ vaim/
COPY migrations/ migrations/
COPY wsgi.py gunicorn.conf.py ./
RUN chmod -R a=rX vaim migrations wsgi.py gunicorn.conf.py
USER vaim
EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
