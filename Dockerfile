FROM nikolaik/python-nodejs:python3.12-nodejs20

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg xvfb sudo screen tmux && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN curl -fsSL -o napcat.sh https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.sh \
    && bash napcat.sh --docker n --cli y --proxy 0 --confirm \
    && rm napcat.sh

COPY *.py ./
COPY face_config.json .
COPY all_output_result_kmj.txt .
COPY sensitive_words.txt .
COPY templates ./templates

COPY start.sh .

RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

EXPOSE 7860

CMD ["bash", "start.sh"]
