
import streamlit as st
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.messages import HumanMessage, AIMessage
from operator import itemgetter

# 벡터 저장소 경로, 컬렉션 이름 (Colab에서 만들 때와 같아야 함)
CHROMA_DIR = "chroma_db"
COLLECTION = "aivle_docs"

# 문서 포맷팅 함수
def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

# Embedding 모델, 검색기
@st.cache_resource
def get_retriever():
    embedding = OpenAIEmbeddings(model="text-embedding-3-small")
    vectorstore = Chroma(
        collection_name=COLLECTION,
        embedding_function=embedding,
        persist_directory=CHROMA_DIR
    )
    return vectorstore.as_retriever(search_kwargs={"k": 3})

# 체인 캐싱
@st.cache_resource
def get_chain():
    retriever = get_retriever()
    s_msg = "당신은 주어진 문서를 기반으로 답변하는 AIVLE School 학습 도우미입니다.\n\n[문서]\n\n{context}"
    h_msg = "{input}"
    messages = [("system", s_msg), ("human", h_msg)]
    prompt = ChatPromptTemplate.from_messages(messages)
    llm = ChatOpenAI(model="gpt-4o-mini", streaming=True, temperature=0)
    rag_chain = (
        {
            "context": itemgetter("input") | retriever | format_docs,
            "input": itemgetter("input")
        }
        | prompt
        | llm
        | StrOutputParser()
    )
    return rag_chain

# RAG 체인 준비
chain = get_chain()

# 페이지 설정
st.set_page_config(page_title="AIVLE School 학습 도우미", page_icon="📘")
st.title("📘 AIVLE School 학습 도우미")

# 세션 상태 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []

# 기존 대화 내용 출력
for message in st.session_state.messages:
    role = "user" if isinstance(message, HumanMessage) else "assistant"
    with st.chat_message(role):
        st.markdown(message.content)

# 사용자 입력 처리
user_input = st.chat_input("메시지를 입력하세요.")
if user_input:
    # 사용자 메시지 표시
    st.chat_message("user").markdown(user_input)

    # 응답 출력
    with st.chat_message("assistant"):
        response_stream = chain.stream({"input": user_input})
        full_response = st.write_stream(response_stream)

    # 대화 기록 저장
    st.session_state.messages.append(HumanMessage(content=user_input))
    st.session_state.messages.append(AIMessage(content=full_response))
