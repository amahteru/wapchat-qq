FROM nikolaik/python-nodejs:python3.9-nodejs20

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg xvfb sudo screen tmux

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh \
    && bash napcat.sh \
    --qq "123456789" \
    --mode ws \
    --proxy 0 \
    --confirm

COPY app.py .
COPY face_config.json .
COPY all_output_result_kmj.txt .
COPY sensitive_words.txt .
COPY templates ./templates

COPY start.sh .

ENV BOT_QQ="123456789"

RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

EXPOSE 7860

CMD ["bash", "start.sh"]
