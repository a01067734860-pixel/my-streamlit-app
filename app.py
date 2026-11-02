
import os
import streamlit as st
from openai import OpenAI

# OpenAI 클라이언트 생성
client = OpenAI()

# 페이지 설정
st.set_page_config(page_title="간단한 챗봇", page_icon="🤖")
st.title("🤖 단순 챗봇")
st.write("대화를 기억하지 않는 단순 챗봇입니다.")

# 사용자 질문
prompt = st.text_input("질문을 입력하세요")

# 질문 버튼
if st.button("질문하기"):
    if prompt.strip() == "":
        st.warning("질문을 입력하세요")
    else:
         with st.spinner("답변 생성 중입니다..."):
            # LLM 호출
            response = client.responses.create(
                model="gpt-4o-mini",
                input=[{"role": "user", "content": prompt}]
            )
            answer = response.output_text

            # 결과 출력
            st.subheader("AI 답변")
            st.write(answer)
