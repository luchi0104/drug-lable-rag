import os

from .provenance import compute_rerank_provenance
# from .provenance import (compute_llm_provenance_cloud, compute_rerank_provenance, DocumentSimilarityAttribution)
from .ScoredCrossEncoderReranker import ScoredCrossEncoderReranker
from .RAGHelper import RAGHelper
from .RAGHelper import formatDocuments

from langchain.retrievers import EnsembleRetriever
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_experimental.text_splitter import SemanticChunker
from .get_embeddings import get_embedding_function
from langchain.prompts import ChatPromptTemplate
from langchain.schema.runnable import RunnablePassthrough
from langchain_community.retrievers import BM25Retriever
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import FlashrankRerank
from langchain_core.output_parsers import StrOutputParser
from langchain_community.cross_encoders import HuggingFaceCrossEncoder

from langchain_community.document_loaders import PyPDFLoader
from langchain_community.document_loaders import JSONLoader
from langchain_community.document_loaders import Docx2txtLoader
from langchain_community.document_loaders import UnstructuredExcelLoader
from langchain_community.document_loaders import UnstructuredPowerPointLoader
from langchain_community.document_loaders import UnstructuredMarkdownLoader
from langchain_community.document_loaders.csv_loader import CSVLoader

from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import AzureChatOpenAI
from langchain_ollama.llms import OllamaLLM

import re
import pickle


def combine_results(inputs):
    if "context" in inputs.keys() and "docs" in inputs.keys():
        return {
            "answer": inputs["answer"],
            "docs": inputs["docs"],
            "context": inputs["context"],
            "question": inputs["question"]
        }
    else:
        return {
            "answer": inputs["answer"],
            "question": inputs["question"]
        }


class RAGHelperCloud(RAGHelper):
    def __init__(self, logger):
        if os.getenv("use_openai") == "True":
            self.llm = ChatOpenAI(
                model=os.getenv("openai_model_name"),
                temperature=0,
                max_tokens=None,
                timeout=None,
                max_retries=2,
            )
        elif os.getenv("use_gemini") == "True":
            self.llm = ChatGoogleGenerativeAI(model=os.getenv("gemini_model_name"),
                                              convert_system_message_to_human=True)
        elif os.getenv("use_azure") == "True":
            self.llm = AzureChatOpenAI(
                openai_api_version=os.environ["AZURE_OPENAI_API_VERSION"],
                azure_deployment=os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"],
            )
        elif os.getenv("use_ollama") == "True":
            self.llm = OllamaLLM(model=os.getenv("ollama_model"))

        self.embeddings = get_embedding_function()

        # Load the data
        self.loadData()

        # Create the RAG chain for determining if we need to fetch new documents
        rag_thread = [
            ('system', os.getenv('rag_fetch_new_instruction')),
            ('human', os.getenv('rag_fetch_new_question'))
        ]
        rag_prompt = ChatPromptTemplate.from_messages(rag_thread)
        rag_llm_chain = rag_prompt | self.llm
        self.rag_fetch_new_chain = (
                {"question": RunnablePassthrough()} |
                rag_llm_chain
        )

        # For provenance
        if os.getenv("provenance_method") == "similarity":
            pass
            # self.attributor = DocumentSimilarityAttribution()

        # Also create the rewrite loop LLM chain, if need be
        self.rewrite_ask_chain = None
        self.rewrite_chain = None
        if os.getenv("use_rewrite_loop") == "True":
            # First the chain to ask the LLM if a rewrite would be required
            rewrite_ask_thread = [
                ('system', os.getenv('rewrite_query_instruction')),
                ('human', os.getenv('rewrite_query_question'))
            ]
            rewrite_ask_prompt = ChatPromptTemplate.from_messages(rewrite_ask_thread)
            rewrite_ask_llm_chain = rewrite_ask_prompt | self.llm
            context_retriever = self.ensemble_retriever
            if os.getenv("rerank") == "True":
                context_retriever = self.rerank_retriever
            self.rewrite_ask_chain = (
                    {"context": context_retriever | formatDocuments, "question": RunnablePassthrough()} |
                    rewrite_ask_llm_chain
            )

            # Next the chain to ask the LLM for the actual rewrite(s)
            rewrite_thread = [
                ('human', os.getenv('rewrite_query_prompt'))
            ]
            rewrite_prompt = ChatPromptTemplate.from_messages(rewrite_thread)
            rewrite_llm_chain = rewrite_prompt | self.llm
            self.rewrite_chain = (
                    {"question": RunnablePassthrough()} |
                    rewrite_llm_chain
            )

    def handle_rewrite(self, user_query):
        # Check if we even need to rewrite or not
        if os.getenv("use_rewrite_loop") == "True":
            # Ask the LLM if we need to rewrite
            response = self.rewrite_ask_chain.invoke(user_query)
            if hasattr(response, 'content'):
                response = response.content
            elif hasattr(response, 'answer'):
                response = response.answer
            elif 'answer' in response:
                response = response["answer"]
            response = re.sub(r'\W+ ', '', response)
            if response.lower().startswith('yes'):
                # Start the rewriting into different alternatives
                response = self.rewrite_chain.invoke(user_query)

                if hasattr(response, 'content'):
                    response = response.content
                elif hasattr(response, 'answer'):
                    response = response.answer
                elif 'answer' in response:
                    response = response["answer"]

                # Show be split by newlines
                return response
            else:
                # We do not need to rewrite
                return user_query
        else:
            return user_query

    # Main function to handle user interaction
    def handle_user_interaction(self, user_query, history):
        if len(history) == 0:
            fetch_new_documents = True
        else:
            # Prompt for LLM
            response = self.rag_fetch_new_chain.invoke(user_query)
            if hasattr(response, 'content'):
                response = response.content
            elif hasattr(response, 'answer'):
                response = response.answer
            elif 'answer' in response:
                response = response["answer"]
            response = re.sub(r'\W+ ', '', response)
            if response.lower().startswith('yes'):
                fetch_new_documents = True
            else:
                fetch_new_documents = False

        # Create prompt template based on whether we have history or not
        thread = [(x["role"], x["content"].replace("{", "(").replace("}", ")")) for x in history]
        if fetch_new_documents:
            thread = []
        if len(thread) == 0:
            thread.append(('system', os.getenv('rag_instruction')))
            thread.append(('human', os.getenv('rag_question_initial')))
        else:
            thread.append(('human', os.getenv('rag_question_followup')))

        # Create prompt from prompt template
        prompt = ChatPromptTemplate.from_messages(thread)

        # Create llm chain
        llm_chain = prompt | self.llm
        if fetch_new_documents:
            # Rewrite the question if needed
            user_query = self.handle_rewrite(user_query)
            context_retriever = self.ensemble_retriever
            if os.getenv("rerank") == "True":
                context_retriever = self.rerank_retriever

            retriever_chain = {
                "docs": context_retriever,
                "context": context_retriever | formatDocuments,
                "question": RunnablePassthrough()
            }
            llm_chain = prompt | self.llm | StrOutputParser()
            rag_chain = (
                    retriever_chain
                    | RunnablePassthrough.assign(
                answer=lambda x: llm_chain.invoke(
                    {"docs": x["docs"], "context": x["context"], "question": x["question"]}
                ))
                    | combine_results
            )
        else:
            retriever_chain = {
                "question": RunnablePassthrough()
            }
            llm_chain = prompt | self.llm | StrOutputParser()
            rag_chain = (
                    retriever_chain
                    | RunnablePassthrough.assign(
                answer=lambda x: llm_chain.invoke(
                    {"question": x["question"]}
                ))
                    | combine_results
            )

        # Check if we need to apply Re2 to mention the question twice
        if os.getenv("use_re2") == "True":
            user_query = f'{user_query}\n{os.getenv("re2_prompt")}{user_query}'

        # Invoke RAG pipeline
        reply = rag_chain.invoke(user_query)

        # See if we need to track provenance
        if fetch_new_documents and os.getenv("provenance_method") in ['rerank', 'attention', 'similarity', 'llm']:
            # Add the user question and the answer to our thread for provenance computation
            answer = reply['answer']
            context = reply['docs']

            # Use the reranker but now on the answer (and potentially query too)
            if os.getenv("provenance_method") == "rerank":
                if not (os.getenv("rerank") == "True"):
                    raise ValueError(
                        "Provenance attribution is set to rerank but reranking is not enabled. Please choose another provenance method or turn on reranking.")
                reranked_docs = compute_rerank_provenance(self.compressor, user_query, reply['docs'], answer)

                # This is a bit of a hassle because reranked_docs is now reordered and we have no definitive key to use because of hybrid search.
                # Note that we can't just return reranked_docs because the LLM may refer to "doc #1" in the order of the original scoring.
                provenance_scores = []
                for doc in context:
                    # Find the document in reranked_docs
                    reranked_score = \
                        [d.metadata['relevance_score'] for d in reranked_docs if d.page_content == doc.page_content][0]
                    provenance_scores.append(reranked_score)
            # See if we need to do similarity-base provenance
            elif os.getenv("provenance_method") == "similarity":
                pass
                # provenance_scores = self.attributor.compute_similarity(user_query, context, answer)
            # See if we need to use LLM-based provenance
            elif os.getenv("provenance_method") == "llm":
                pass
                # provenance_scores = compute_llm_provenance_cloud(self.llm, user_query, context, answer)

            # Add the provenance scores
            for i, score in enumerate(provenance_scores):
                reply['docs'][i].metadata['provenance'] = score

        return (thread, reply)

    def addDocument(self, filename):
        filename = os.path.join(os.getenv("data_directory"), filename)
        if filename.lower().endswith('pdf'):
            docs = PyPDFLoader(filename).load()
        if filename.lower().endswith('json'):
            docs = JSONLoader(
                file_path=filename,
                jq_schema=os.getenv("json_schema"),
                text_content=os.getenv("json_text_content") == "True",
            ).load()
        if filename.lower().endswith('csv'):
            docs = CSVLoader(filename).load()
        if filename.lower().endswith('docx'):
            docs = Docx2txtLoader(filename).load()
        if filename.lower().endswith('xlsx'):
            docs = UnstructuredExcelLoader(filename).load()
        if filename.lower().endswith('pptx'):
            docs = UnstructuredPowerPointLoader(filename).load()
        if filename.lower().endswith('md'):
            docs = UnstructuredMarkdownLoader(filename).load()

        new_docs = docs

        if os.getenv('splitter') == 'RecursiveCharacterTextSplitter':
            self.text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=int(os.getenv('chunk_size')),
                chunk_overlap=int(os.getenv('chunk_overlap')),
                length_function=len,
                keep_separator=True,
                is_separator_regex=True,
                separators=[
                    r'\n\s*\n',
                    "\n \n",
                    "\n\n",
                    "\n",
                    ".",
                    "!",
                    "?",
                    " ",
                    ",",
                    "\u200b",  # Zero-width space
                    "\uff0c",  # Fullwidth comma
                    "\u3001",  # Ideographic comma
                    "\uff0e",  # Fullwidth full stop
                    "\u3002",  # Ideographic full stop
                    "",
                ],
            )
        elif os.getenv('splitter') == 'SemanticChunker':
            breakpoint_threshold_amount = None
            number_of_chunks = None
            if os.getenv('breakpoint_threshold_amount') != 'None':
                breakpoint_threshold_amount = float(os.getenv('breakpoint_threshold_amount'))
            if os.getenv('number_of_chunks') != 'None':
                number_of_chunks = int(os.getenv('number_of_chunks'))
            self.text_splitter = SemanticChunker(
                self.embeddings,
                breakpoint_threshold_type=os.getenv('breakpoint_threshold_type'),
                breakpoint_threshold_amount=breakpoint_threshold_amount,
                number_of_chunks=number_of_chunks
            )

        new_chunks = self.text_splitter.split_documents(new_docs)

        self.chunked_documents = self.chunked_documents + new_chunks

        invalid_chars = r'<>:"/\|?*'
        valid_filename = re.sub(f"[{re.escape(invalid_chars)}]", "_",
                                os.getenv('vector_store'))
        # Store these too, for our sparse DB
        with open(f"{valid_filename}_sparse.pickle", 'wb') as f:
            pickle.dump(self.chunked_documents, f)

        # Add to vector DB
        self.db.add_documents(new_chunks)

        # Add to BM25
        bm25_retriever = BM25Retriever.from_texts(
            [x.page_content for x in self.chunked_documents],
            metadatas=[x.metadata for x in self.chunked_documents]
        )

        # Update full retriever too
        retriever = self.db.as_retriever(search_type="mmr", search_kwargs={'k': int(os.getenv('vector_store_k'))})
        self.ensemble_retriever = EnsembleRetriever(
            retrievers=[bm25_retriever, retriever], weights=[0.5, 0.5]
        )

        if os.getenv("rerank") == "True":
            if os.getenv("rerank_model") == "flashrank":
                self.compressor = FlashrankRerank(top_n=int(os.getenv("rerank_k")),
                                                  model=os.getenv("flashrank_model", None))
            else:
                self.compressor = ScoredCrossEncoderReranker(
                    model=HuggingFaceCrossEncoder(model_name=os.getenv("rerank_model")),
                    top_n=int(os.getenv("rerank_k"))
                )

            self.rerank_retriever = ContextualCompressionRetriever(
                base_compressor=self.compressor, base_retriever=self.ensemble_retriever
            )
