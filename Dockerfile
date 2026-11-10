FROM nikolaik/python-nodejs:python3.9-nodejs20

WORKDIR /app

# 1. 安装底层依赖
RUN apt-get update && apt-get install -y ffmpeg xvfb sudo screen tmux

# 2. 安装 Python 依赖
COPY requirements.txt .
RUN pip install -r requirements.txt

# 3. 下载并直装 NapCatQQ
RUN curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh \
    && bash napcat.sh \
    --qq "123456789" \
    --mode ws \
    --proxy 0 \
    --confirm

# 4. 把代码和配置文件拷贝进来
COPY app.py .
COPY face_config.json .
COPY all_output_result_kmj.txt .
COPY sensitive_words.txt .

COPY start.sh .

# 5. 机器人 QQ 号由运行时环境变量 BOT_QQ 覆盖；去掉 Windows 换行符以防脚本无法执行
ENV BOT_QQ="123456789"

RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

EXPOSE 7860

CMD ["bash", "start.sh"]
