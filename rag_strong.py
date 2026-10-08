import os,json, re
import PyPDF2
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

DB_FILE = "rag_db.json"

def clean_text(text):
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def load_and_chunk_pdf(pdf_path, chunk_size=600, overlap=100):
    reader = PyPDF2.PdfReader(pdf_path)
    full_text = ""
    for page in reader.pages:
        t = page.extract_text()
        if t:
            full_text += t + "\n"
    full_text = clean_text(full_text)
    chunks = []
    start = 0
    while start < len(full_text):
        end = start + chunk_size
        piece = full_text[start:end]
        if len(piece) > 100:
            chunks.append(piece)
        start = end - overlap
    return chunks 

def build_or_load_db(pdf_path):
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if data.get("pdf_name") == pdf_path:
                print("RAG: using cached DB")
                return data["chunks"]
            print("RAG: building new DB...")
            chunks = load_and_chunk_pdf(pdf_path)
            with open(DB_FILE, 'w', encoding='utf-8') as f:
                json.dump({"pdf_name": pdf_path, "chunks": chunks}, f, ensure_ascii=False)
                return chunks

class StrongRAG:
    def __init__(self, pdf_path="catalogue.pdf"):
        self.chunks = build_or_load_db(pdf_path)
        self.vectorizer = TfidfVectorizer(ngram_range=(1,2))
        self.vectors = self.vectorizer.fit_transform(self.chunks)
        print(f"RAG: ready with {len(self.chunks)} chunks")

def search(self, question, top_k=2):
    q_vec = self.vectorizer.transform([question])
    scores = cosine_similarity(q_vec, self.vectors)[0]
    best_idx = np.argsort(scores)[-top_k:][::-1]
    results = []
    for idx in best_idx:
        if scores[idx] > 0.15:
            results.append(self.chunks[idx])
            return results                                