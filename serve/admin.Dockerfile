FROM python:3.12.11-alpine3.22
RUN pip install --no-cache-dir starlette==1.7.0 uvicorn==0.54.0 httpx==0.28.1
WORKDIR /app
COPY unadcast/admin.py .
# A new named volume takes this ownership, so the unprivileged user can write the show list.
RUN mkdir /data && chown nobody /data
USER nobody
CMD ["uvicorn", "--factory", "admin:main", "--host", "0.0.0.0", "--port", "8000"]
