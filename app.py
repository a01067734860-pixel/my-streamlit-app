
import time
import streamlit as st
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

# 페이지 설정
st.set_page_config(page_title="LLM 챗봇", page_icon="💬", layout="centered")
st.title("💬 LLM과의 대화")

# LLM 구성
llm = ChatOpenAI(model="gpt-4o-mini")

# 세션 상태 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []

# 기존 대화 출력
for message in st.session_state.messages:
    role = "user" if isinstance(message, HumanMessage) else "assistant"
    with st.chat_message(role):
        st.markdown(message.content)

# 사용자 입력
user_input = st.chat_input("메시지를 입력하세요...")

if user_input:
    # 사용자 메시지 출력 및 저장
    st.chat_message("user").markdown(user_input)
    st.session_state.messages.append(HumanMessage(content=user_input))

    # 질문과 답변
    response = llm.invoke(st.session_state.messages)

    # 타이핑 효과로 출력
    with st.chat_message("assistant"):
        response_container = st.empty()
        full_response = ""
        for char in response.content:
            full_response += char
            response_container.markdown(full_response + "▌")
            time.sleep(0.01)
        response_container.markdown(full_response)

    # 대화 기록 저장
    st.session_state.messages.append(AIMessage(content=full_response))
