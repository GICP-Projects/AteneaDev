FROM python:3.8.10
LABEL stage=builder
ENV PYTHONUNBUFFERED 1
RUN mkdir /backend
COPY . /backend/

# Crear un entorno virtual Python y activarlo
RUN python3 -m venv /backend/venv
ENV PATH="/backend/venv/bin:$PATH"

WORKDIR /backend

# Actualizar pip e instalar las dependencias en un solo RUN
RUN python3 -m pip install --upgrade pip && \
    pip3 install -r requirements.txt --no-cache-dir

# Download required models
RUN chmod +x download.sh
RUN ./download.sh > installation_log.txt
#RUN cat installation_log.txt

# RUN API REST
EXPOSE 8000
ENTRYPOINT ["uvicorn"]
CMD ["src.api:app", "--host", "0.0.0.0"]

