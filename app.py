
import streamlit as st
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.messages import HumanMessage, AIMessage
from operator import itemgetter

# 문서 포맷팅 함수
def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

# 체인 캐싱
@st.cache_resource
def get_chain():
    # Embedding 모델
    embedding = OpenAIEmbeddings(model="text-embedding-3-small")

    # 벡터 저장소
    vectorstore = Chroma(
        collection_name="hr_rules",
        embedding_function=embedding,
        persist_directory="chroma_db"
    )

    # 검색기 구성
    retriever = vectorstore.as_retriever()

    # 체인 구성
    s_msg = """
    1. 반드시 제공된 문서 내용(context)에 근거하여 답변하세요.
    2. 문서에 없는 내용은 추측하지 말고 "업로드된 문서에서 확인되지 않습니다."라고 답하세요.
    3. 답변은 간결하고 명확하게 작성하세요.
    [문서]
    {context}"""
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
st.set_page_config(page_title="RAG 챗봇", page_icon="🏢")
st.title("🏢 인사규정 챗봇")

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
