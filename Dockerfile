FROM nikolaik/python-nodejs:python3.12-nodejs20

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg xvfb sudo screen tmux && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN curl -fsSL -o napcat.sh https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.sh \
    && bash napcat.sh --docker n --cli y --proxy 0 --confirm \
    && rm napcat.sh

RUN mkdir -p data && for f in ip2region_v4.xdb ip2region_v6.xdb; do \
        curl -fsSL -o data/$f https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data/$f; \
    done

COPY *.py ./
COPY face_config.json .
COPY country_zh.json .
COPY all_output_result_kmj.txt .
COPY sensitive_words.txt .
COPY templates ./templates

COPY start.sh .

RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

EXPOSE 7860

CMD ["bash", "start.sh"]
