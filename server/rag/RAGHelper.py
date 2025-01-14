import os
import re

from tqdm import tqdm
import hashlib

from .ScoredCrossEncoderReranker import ScoredCrossEncoderReranker

from langchain_core.documents.base import Document
from langchain.retrievers import EnsembleRetriever
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_experimental.text_splitter import SemanticChunker
from langchain_community.vectorstores import Chroma
from langchain_community.retrievers import BM25Retriever
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import FlashrankRerank
from langchain_community.cross_encoders import HuggingFaceCrossEncoder

from langchain_community.document_loaders import PyPDFLoader, UnstructuredMarkdownLoader
from langchain_community.document_loaders import PyPDFDirectoryLoader
from langchain_community.document_loaders import JSONLoader
from langchain_community.document_loaders import DirectoryLoader
from langchain_community.document_loaders import Docx2txtLoader
from langchain_community.document_loaders import TextLoader
from langchain_community.document_loaders import UnstructuredExcelLoader
from langchain_community.document_loaders import UnstructuredPowerPointLoader
from langchain_community.document_loaders.csv_loader import CSVLoader

from lxml import etree
import pickle


# Make documents look a bit better than default
def formatDocuments(docs):
    doc_strings = []
    for i, doc in enumerate(docs):
        metadata_string = ", ".join([f"{md}: {doc.metadata[md]}" for md in doc.metadata.keys()])
        doc_strings.append(f"Document {i} content: {doc.page_content}\nDocument {i} metadata: {metadata_string}")
    return "\n\n<NEWDOC>\n\n".join(doc_strings)


def extract_source(filename):
    # 移除路徑和文件擴展名
    base_name = filename.split("\\")[-1].replace('.md', '')

    # 使用正則表達式分割文件名
    parts = re.split(r'[\-]', base_name)

    # 過濾掉空字符串
    parts = [part.strip()[:30] for part in parts if part.strip()]
    return " ".join(parts)


class RAGHelper:
    # Loads the data and chunks it into an ensemble retriever
    def loadData(self):
        persist_directory = f"{os.getenv('persist_directory')}"
        document_chunks_pickle = f"{os.getenv('document_chunks_pickle')}"

        if os.path.exists(document_chunks_pickle):
            with open(document_chunks_pickle, 'rb') as f:
                self.chunked_documents = pickle.load(f)
        else:
            # Load PDF files if need be
            docs = []
            data_dir = os.getenv('data_directory')
            file_types = os.getenv("file_types").split(",")
            if "pdf" in file_types:
                loader = PyPDFDirectoryLoader(data_dir)
                docs = docs + loader.load()
            # Load JSON
            if "json" in file_types:
                text_content = True
                if os.getenv("json_text_content").lower() == 'false':
                    text_content = False
                loader_kwargs = {
                    'jq_schema': os.getenv("json_schema"),
                    'text_content': text_content
                }
                loader = DirectoryLoader(
                    path=data_dir,
                    glob="*.json",
                    loader_cls=JSONLoader,
                    loader_kwargs=loader_kwargs,
                    recursive=True,
                    show_progress=True,
                )
                docs = docs + loader.load()
            # Load TXT
            if "txt" in file_types:
                loader = DirectoryLoader(
                    path=data_dir,
                    glob="*.txt",
                    loader_cls=TextLoader,
                    recursive=True,
                    show_progress=True,
                )
                docs = docs + loader.load()
            # Load CSV
            if "csv" in file_types:
                loader = DirectoryLoader(
                    path=data_dir,
                    glob="*.csv",
                    loader_cls=CSVLoader,
                    recursive=True,
                    show_progress=True,
                )
                docs = docs + loader.load()
            # Load MS Word
            if "docx" in file_types:
                loader = DirectoryLoader(
                    path=data_dir,
                    glob="*.docx",
                    loader_cls=Docx2txtLoader,
                    recursive=True,
                    show_progress=True,
                )
                docs = docs + loader.load()
            # Load MS Excel
            if "xlsx" in file_types:
                loader = DirectoryLoader(
                    path=data_dir,
                    glob="*.xlsx",
                    loader_cls=UnstructuredExcelLoader,
                    recursive=True,
                    show_progress=True,
                )
                docs = docs + loader.load()
            # load md
            if "md" in file_types:
                loader = DirectoryLoader(
                    path=data_dir,
                    glob="*.md",
                    loader_cls=UnstructuredMarkdownLoader,
                    recursive=True,
                    show_progress=True,
                )
                docs = docs + loader.load()
            # Load MS PPT
            if "pptx" in file_types:
                loader = DirectoryLoader(
                    path=data_dir,
                    glob="*.pptx",
                    loader_cls=UnstructuredPowerPointLoader,
                    recursive=True,
                    show_progress=True,
                )
                docs = docs + loader.load()
            # Load XML, which is nasty
            if "xml" in file_types:
                loader = DirectoryLoader(
                    path=data_dir,
                    glob="*.xml",
                    loader_cls=TextLoader,
                    recursive=True,
                    show_progress=True,
                )
                xmldocs = loader.load()
                newdocs = []
                for index, doc in enumerate(xmldocs):
                    xmltree = etree.fromstring(doc.page_content.encode('utf-8'))
                    elements = xmltree.xpath(os.getenv("xml_xpath"))
                    elements = [etree.tostring(element, pretty_print=True).decode() for element in elements]
                    metadata = doc.metadata
                    metadata['index'] = index
                    newdocs = newdocs + [Document(page_content=doc, metadata=metadata) for doc in elements]
                docs = docs + newdocs

            if os.getenv('splitter') == 'RecursiveCharacterTextSplitter':
                if os.getenv("use_blank_line_as_separator") == "True":
                    self.text_splitter = RecursiveCharacterTextSplitter(
                        chunk_size=int(os.getenv('chunk_size')),
                        chunk_overlap=int(os.getenv('chunk_overlap')),
                        length_function=len,
                        keep_separator=True,
                        separators=[
                            r'\n\s*\n',
                            r"\n \n",
                            r"\n\n",
                            r"\n",
                            r" ",
                        ],
                        is_separator_regex=True
                    )
                else:
                    self.text_splitter = RecursiveCharacterTextSplitter(
                        chunk_size=int(os.getenv('chunk_size')),
                        chunk_overlap=int(os.getenv('chunk_overlap')),
                        length_function=len,
                        keep_separator=True,
                        separators=[
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

            # Add a hash as ID to each document chunk's metadata and source to content
            self.chunked_documents = [
                Document(page_content=extract_source(doc.metadata['source']) + doc.page_content,
                         metadata={**doc.metadata, 'id': hashlib.md5(doc.page_content.encode()).hexdigest()})
                for doc in self.text_splitter.split_documents(docs)
            ]

            # Store the chunks
            with open(document_chunks_pickle, 'wb') as f:
                pickle.dump(self.chunked_documents, f)

        if os.getenv("vector_store") == "chroma":
            if os.getenv("vector_store_initial_load") == "True":
                self.db = Chroma(
                    embedding_function=self.embeddings,
                    persist_directory=persist_directory,
                    collection_name=os.getenv("vector_store_collection"),
                )

                # Add the documents 1 by 1 so we can track progress
                with tqdm(total=len(self.chunked_documents), desc="Vectorizing documents") as pbar:
                    for d in self.chunked_documents:
                        self.db.add_documents([d])
                        pbar.update(1)
            else:
                # Load chunked documents into the Milvus index
                self.db = Chroma(
                    embedding_function=self.embeddings,
                    persist_directory=persist_directory,
                    collection_name=os.getenv("vector_store_collection"),
                )

            # When we use Chroma, we have an in-memory BM25 retriever
            self.sparse_retriever = BM25Retriever.from_texts(
                [x.page_content for x in self.chunked_documents],
                metadatas=[x.metadata for x in self.chunked_documents]
            )
        else:
            raise Exception(
                "Only chroma are supported as vector stores! Please set vector_store in your .env.en.postgres file.")

        # Set up the vector retriever
        retriever = self.db.as_retriever(
            search_type="mmr", search_kwargs={'k': int(os.getenv("vector_store_k"))}
        )

        # Now combine them to do hybrid retrieval
        self.ensemble_retriever = EnsembleRetriever(
            retrievers=[self.sparse_retriever, retriever], weights=[0.5, 0.5]
        )
        # Set up the reranker
        self.rerank_retriever = None
        if os.getenv("rerank") == "True":
            if os.getenv("rerank_model") == "flashrank":
                model_name = os.getenv("flashrank_model", None)
                self.compressor = FlashrankRerank(top_n=int(os.getenv("rerank_k")), model=model_name)
            else:
                self.compressor = ScoredCrossEncoderReranker(
                    model=HuggingFaceCrossEncoder(model_name=os.getenv("rerank_model")),
                    top_n=int(os.getenv("rerank_k"))
                )

            self.rerank_retriever = ContextualCompressionRetriever(
                base_compressor=self.compressor, base_retriever=self.ensemble_retriever
            )

    # no use
    def addDocument(self, filename):
        if filename.lower().endswith('pdf'):
            docs = PyPDFLoader(filename).load()
        if filename.lower().endswith('json'):
            docs = JSONLoader(
                file_path=filename,
                jq_schema=os.getenv("json_schema"),
                text_content=os.getenv("json_text_content") == "True",
            ).load()
        if filename.lower().endswith('txt'):
            docs = TextLoader(filename).load()
        if filename.lower().endswith('csv'):
            docs = CSVLoader(filename).load()
        if filename.lower().endswith('docx'):
            docs = Docx2txtLoader(filename).load()
        if filename.lower().endswith('xlsx'):
            docs = UnstructuredExcelLoader(filename).load()
        if filename.lower().endswith('pptx'):
            docs = UnstructuredPowerPointLoader(filename).load()

        # Skills and personality are global and don't work on chunks, so do them first
        new_docs = []

        for doc in docs:
            # Get the skills by using the LLM and attach to the doc
            skills = self.parseCV(doc)
            doc.metadata['skills'] = skills
            # Also get the personality
            doc.metadata['personality'] = self.personality_predictor.predict(doc)
            new_docs.append(doc)

        if os.getenv('splitter') == 'RecursiveCharacterTextSplitter':
            self.text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=int(os.getenv('chunk_size')),
                chunk_overlap=int(os.getenv('chunk_overlap')),
                length_function=len,
                keep_separator=True,
                separators=[
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
        # Store these too, for our sparse DB
        with open(f"{os.getenv('vector_store_uri')}_sparse.pickle", 'wb') as f:
            pickle.dump(self.chunked_documents, f)

        # Add to vector DB
        self.db.add_documents(new_chunks)

        # For postgres, add to sparse db too
        if os.getenv("vector_store") == "postgres":
            self.sparse_retriever.add_documents(new_chunks)
        else:
            # Recreate the in-memory store
            self.sparse_retriever = BM25Retriever.from_texts(
                [x.page_content for x in self.chunked_documents],
                metadatas=[x.metadata for x in self.chunked_documents]
            )

            # Update full retriever too
            retriever = self.db.as_retriever(search_type="mmr", search_kwargs={'k': int(os.getenv('vector_store_k'))})
            self.ensemble_retriever = EnsembleRetriever(
                retrievers=[self.sparse_retriever, retriever], weights=[0.5, 0.5]
            )

        if os.getenv("rerank") == "True":
            if os.getenv("rerank_model") == "flashrank":
                self.compressor = FlashrankRerank(top_n=int(os.getenv("rerank_k")))
            else:
                self.compressor = ScoredCrossEncoderReranker(
                    model=HuggingFaceCrossEncoder(model_name=os.getenv("rerank_model")),
                    top_n=int(os.getenv("rerank_k"))
                )

            self.rerank_retriever = ContextualCompressionRetriever(
                base_compressor=self.compressor, base_retriever=self.ensemble_retriever
            )
