FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl libgcrypt20 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o napcat.sh https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.sh \
    && bash napcat.sh --docker n --cli n --proxy 0 --confirm \
    && rm napcat.sh && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p data && for f in ip2region_v4.xdb ip2region_v6.xdb; do \
        curl -fsSL -o data/$f https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data/$f; \
    done

COPY *.py face_config.json country_zh.json all_output_result_kmj.txt sensitive_words.txt start.sh ./
COPY templates ./templates

EXPOSE 7860

CMD ["bash", "start.sh"]
