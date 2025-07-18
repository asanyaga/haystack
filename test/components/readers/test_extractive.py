# SPDX-FileCopyrightText: 2022-present deepset GmbH <info@deepset.ai>
#
# SPDX-License-Identifier: Apache-2.0

import logging
from math import ceil, exp
from typing import List
from unittest.mock import Mock, patch

import pytest
import torch
from _pytest.monkeypatch import MonkeyPatch
from transformers import pipeline

from haystack import Document, ExtractedAnswer
from haystack.components.readers import ExtractiveReader
from haystack.utils import Secret
from haystack.utils.device import ComponentDevice, DeviceMap


@pytest.fixture()
def initialized_token(monkeypatch: MonkeyPatch) -> Secret:
    monkeypatch.setenv("HF_API_TOKEN", "secret-token")

    return Secret.from_env_var(["HF_API_TOKEN", "HF_TOKEN"], strict=False)


@pytest.fixture
def mock_tokenizer():
    def mock_tokenize(
        texts: List[str],
        text_pairs: List[str],
        padding: bool,
        truncation: bool,
        max_length: int,
        return_tensors: str,
        return_overflowing_tokens: bool,
        stride: int,
    ):
        assert padding
        assert truncation
        assert return_tensors == "pt"
        assert return_overflowing_tokens

        tokens = Mock()

        num_splits = [ceil(len(text + pair) / max_length) for text, pair in zip(texts, text_pairs)]
        tokens.overflow_to_sample_mapping = [i for i, num in enumerate(num_splits) for _ in range(num)]
        num_samples = sum(num_splits)
        tokens.encodings = [Mock() for _ in range(num_samples)]
        sequence_ids = [0] * 16 + [1] * 16 + [None] * (max_length - 32)
        for encoding in tokens.encodings:
            encoding.sequence_ids = sequence_ids
            encoding.token_to_chars = lambda i: (i - 16, i - 15)
        tokens.input_ids = torch.zeros(num_samples, max_length, dtype=torch.int)
        attention_mask = torch.zeros(num_samples, max_length, dtype=torch.int)
        attention_mask[:32] = 1
        tokens.attention_mask = attention_mask
        return tokens

    with patch("haystack.components.readers.extractive.AutoTokenizer.from_pretrained") as tokenizer:
        tokenizer.return_value = mock_tokenize
        yield tokenizer


@pytest.fixture()
def mock_reader(mock_tokenizer):
    class MockModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.hf_device_map = {"": "cpu:0"}

        def forward(self, input_ids, attention_mask, *args, **kwargs):
            assert input_ids.device == torch.device("cpu")
            assert attention_mask.device == torch.device("cpu")
            start = torch.zeros(input_ids.shape[:2])
            end = torch.zeros(input_ids.shape[:2])
            start[:, 27] = 1
            end[:, 31] = 1
            end[:, 32] = 1
            prediction = Mock()
            prediction.start_logits = start
            prediction.end_logits = end
            return prediction

    with patch("haystack.components.readers.extractive.AutoModelForQuestionAnswering.from_pretrained") as model:
        model.return_value = MockModel()
        reader = ExtractiveReader(model="mock-model", device=ComponentDevice.from_str("cpu"))
        reader.warm_up()
        return reader


example_queries = ["Who is the chancellor of Germany?", "Who is the head of the department?"]
example_documents = [
    [
        Document(content="Angela Merkel was the chancellor of Germany."),
        Document(content="Olaf Scholz is the chancellor of Germany"),
        Document(content="Jerry is the head of the department.", meta={"page_number": 3}),
    ]
] * 2


def test_to_dict(initialized_token: Secret):
    component = ExtractiveReader("my-model", token=initialized_token, model_kwargs={"torch_dtype": torch.float16})
    data = component.to_dict()

    assert data == {
        "type": "haystack.components.readers.extractive.ExtractiveReader",
        "init_parameters": {
            "model": "my-model",
            "device": None,
            "token": {"env_vars": ["HF_API_TOKEN", "HF_TOKEN"], "strict": False, "type": "env_var"},
            "top_k": 20,
            "score_threshold": None,
            "max_seq_length": 384,
            "stride": 128,
            "max_batch_size": None,
            "answers_per_seq": None,
            "no_answer": True,
            "calibration_factor": 0.1,
            "model_kwargs": {
                "torch_dtype": "torch.float16",
                "device_map": ComponentDevice.resolve_device(None).to_hf(),
            },  # torch_dtype is correctly serialized
        },
    }


def test_to_dict_no_token():
    component = ExtractiveReader("my-model", token=None, model_kwargs={"torch_dtype": torch.float16})
    data = component.to_dict()

    assert data == {
        "type": "haystack.components.readers.extractive.ExtractiveReader",
        "init_parameters": {
            "model": "my-model",
            "device": None,
            "token": None,
            "top_k": 20,
            "score_threshold": None,
            "max_seq_length": 384,
            "stride": 128,
            "max_batch_size": None,
            "answers_per_seq": None,
            "no_answer": True,
            "calibration_factor": 0.1,
            "model_kwargs": {
                "torch_dtype": "torch.float16",
                "device_map": ComponentDevice.resolve_device(None).to_hf(),
            },  # torch_dtype is correctly serialized
        },
    }


def test_to_dict_empty_model_kwargs(initialized_token: Secret):
    component = ExtractiveReader("my-model", token=initialized_token)
    data = component.to_dict()

    assert data == {
        "type": "haystack.components.readers.extractive.ExtractiveReader",
        "init_parameters": {
            "model": "my-model",
            "device": None,
            "token": {"env_vars": ["HF_API_TOKEN", "HF_TOKEN"], "strict": False, "type": "env_var"},
            "top_k": 20,
            "score_threshold": None,
            "max_seq_length": 384,
            "stride": 128,
            "max_batch_size": None,
            "answers_per_seq": None,
            "no_answer": True,
            "calibration_factor": 0.1,
            "model_kwargs": {"device_map": ComponentDevice.resolve_device(None).to_hf()},
        },
    }


@pytest.mark.parametrize(
    "device_map,expected",
    [
        ("auto", "auto"),
        ("cpu:0", ComponentDevice.from_str("cpu:0").to_hf()),
        ({"": "cpu:0"}, ComponentDevice.from_multiple(DeviceMap.from_hf({"": "cpu:0"})).to_hf()),
    ],
)
def test_to_dict_device_map(device_map, expected):
    component = ExtractiveReader("my-model", model_kwargs={"device_map": device_map})
    data = component.to_dict()

    assert data == {
        "type": "haystack.components.readers.extractive.ExtractiveReader",
        "init_parameters": {
            "model": "my-model",
            "device": None,
            "token": {"env_vars": ["HF_API_TOKEN", "HF_TOKEN"], "strict": False, "type": "env_var"},
            "top_k": 20,
            "score_threshold": None,
            "max_seq_length": 384,
            "stride": 128,
            "max_batch_size": None,
            "answers_per_seq": None,
            "no_answer": True,
            "calibration_factor": 0.1,
            "model_kwargs": {"device_map": expected},
        },
    }


def test_from_dict():
    data = {
        "type": "haystack.components.readers.extractive.ExtractiveReader",
        "init_parameters": {
            "model": "my-model",
            "device": None,
            "token": {"env_vars": ["HF_API_TOKEN", "HF_TOKEN"], "strict": False, "type": "env_var"},
            "top_k": 20,
            "score_threshold": None,
            "max_seq_length": 384,
            "stride": 128,
            "max_batch_size": None,
            "answers_per_seq": None,
            "no_answer": True,
            "calibration_factor": 0.1,
            "model_kwargs": {"torch_dtype": "torch.float16"},
        },
    }

    component = ExtractiveReader.from_dict(data)
    assert component.model_name_or_path == "my-model"
    assert component.device is None
    assert component.token == Secret.from_env_var(["HF_API_TOKEN", "HF_TOKEN"], strict=False)
    assert component.top_k == 20
    assert component.score_threshold is None
    assert component.max_seq_length == 384
    assert component.stride == 128
    assert component.max_batch_size is None
    assert component.answers_per_seq is None
    assert component.no_answer
    assert component.calibration_factor == 0.1
    # torch_dtype is correctly deserialized
    assert component.model_kwargs == {
        "torch_dtype": torch.float16,
        "device_map": ComponentDevice.resolve_device(None).to_hf(),
    }


def test_from_dict_no_default_parameters():
    data = {"type": "haystack.components.readers.extractive.ExtractiveReader", "init_parameters": {}}

    component = ExtractiveReader.from_dict(data)
    assert component.model_name_or_path == "deepset/roberta-base-squad2-distilled"
    assert component.device is None
    assert component.token == Secret.from_env_var(["HF_API_TOKEN", "HF_TOKEN"], strict=False)
    assert component.top_k == 20
    assert component.score_threshold is None
    assert component.max_seq_length == 384
    assert component.stride == 128
    assert component.max_batch_size is None
    assert component.answers_per_seq is None
    assert component.no_answer
    assert component.calibration_factor == 0.1
    assert component.overlap_threshold == 0.01
    assert component.model_kwargs == {"device_map": ComponentDevice.resolve_device(None).to_hf()}


def test_from_dict_no_token():
    data = {
        "type": "haystack.components.readers.extractive.ExtractiveReader",
        "init_parameters": {
            "model": "my-model",
            "device": None,
            "token": None,
            "top_k": 20,
            "score_threshold": None,
            "max_seq_length": 384,
            "stride": 128,
            "max_batch_size": None,
            "answers_per_seq": None,
            "no_answer": True,
            "calibration_factor": 0.1,
            "model_kwargs": {"torch_dtype": "torch.float16"},
        },
    }

    component = ExtractiveReader.from_dict(data)
    assert component.token is None


def test_run_no_docs(mock_reader: ExtractiveReader):
    mock_reader.warm_up()
    assert mock_reader.run(query="hello", documents=[]) == {"answers": []}


def test_output(mock_reader: ExtractiveReader):
    answers = mock_reader.run(example_queries[0], example_documents[0], top_k=3)["answers"]
    doc_ids = set()
    no_answer_prob = 1
    for doc, answer in zip(example_documents[0], answers[:3]):
        assert answer.document_offset is not None
        assert answer.document_offset.start == 11
        assert answer.document_offset.end == 16
        assert doc.content is not None
        assert answer.data == doc.content[11:16]
        assert answer.score == pytest.approx(1 / (1 + exp(-2 * mock_reader.calibration_factor)))
        no_answer_prob *= 1 - answer.score
        doc_ids.add(doc.id)
    assert len(doc_ids) == 3
    assert answers[-1].score == pytest.approx(no_answer_prob)


def test_flatten_documents(mock_reader: ExtractiveReader):
    queries, docs, query_ids = mock_reader._flatten_documents(example_queries, example_documents)
    i = 0
    for j, query in enumerate(example_queries):
        for doc in example_documents[j]:
            assert queries[i] == query
            assert docs[i] == doc
            assert query_ids[i] == j
            i += 1
    assert len(docs) == len(queries) == len(query_ids) == i


def test_preprocess(mock_reader: ExtractiveReader):
    _, _, seq_ids, _, query_ids, doc_ids = mock_reader._preprocess(
        queries=example_queries * 3, documents=example_documents[0], max_seq_length=384, query_ids=[1, 1, 1], stride=0
    )
    expected_seq_ids = torch.full((3, 384), -1, dtype=torch.int)
    expected_seq_ids[:, :16] = 0
    expected_seq_ids[:, 16:32] = 1
    assert torch.equal(seq_ids, expected_seq_ids)
    assert query_ids == [1, 1, 1]
    assert doc_ids == [0, 1, 2]


def test_preprocess_splitting(mock_reader: ExtractiveReader):
    _, _, seq_ids, _, query_ids, doc_ids = mock_reader._preprocess(
        queries=example_queries * 4,
        documents=example_documents[0] + [Document(content="a" * 64)],
        max_seq_length=96,
        query_ids=[1, 1, 1, 1],
        stride=0,
    )
    assert seq_ids.shape[0] == 5
    assert query_ids == [1, 1, 1, 1, 1]
    assert doc_ids == [0, 1, 2, 3, 3]


def test_postprocess(mock_reader: ExtractiveReader):
    start = torch.zeros((2, 8))
    start[0, 3] = 4
    start[0, 1] = 5  # test attention_mask
    start[0, 4] = 3
    start[1, 2] = 1

    end = torch.zeros((2, 8))
    end[0, 1] = 5  # test attention_mask
    end[0, 2] = 4  # test that end can't be before start
    end[0, 3] = 3
    end[0, 4] = 2
    end[1, :] = -10
    end[1, 4] = -1

    sequence_ids = torch.ones((2, 8))
    attention_mask = torch.ones((2, 8))
    attention_mask[0, :2] = 0
    encoding = Mock()
    encoding.token_to_chars = lambda i: (int(i), int(i) + 1)

    start_candidates, end_candidates, probs = mock_reader._postprocess(
        start=start,
        end=end,
        sequence_ids=sequence_ids,
        attention_mask=attention_mask,
        answers_per_seq=3,
        encodings=[encoding, encoding],
    )

    assert len(start_candidates) == len(end_candidates) == len(probs) == 2
    assert len(start_candidates[0]) == len(end_candidates[0]) == len(probs[0]) == 3
    assert start_candidates[0][0] == 3
    assert end_candidates[0][0] == 4
    assert start_candidates[0][1] == 3
    assert end_candidates[0][1] == 5
    assert start_candidates[0][2] == 4
    assert end_candidates[0][2] == 5
    assert probs[0][0] == pytest.approx(1 / (1 + exp(-7 * mock_reader.calibration_factor)))
    assert probs[0][1] == pytest.approx(1 / (1 + exp(-6 * mock_reader.calibration_factor)))
    assert probs[0][2] == pytest.approx(1 / (1 + exp(-5 * mock_reader.calibration_factor)))
    assert start_candidates[1][0] == 2
    assert end_candidates[1][0] == 5
    assert probs[1][0] == pytest.approx(1 / 2)


def test_nest_answers(mock_reader: ExtractiveReader):
    start = list(range(5))
    end = [i + 5 for i in start]
    start = [start] * 6  # type: ignore
    end = [end] * 6  # type: ignore
    probabilities = torch.arange(5).unsqueeze(0) / 5 + torch.arange(6).unsqueeze(-1) / 25
    query_ids = [0] * 3 + [1] * 3
    document_ids = list(range(3)) * 2
    nested_answers = mock_reader._nest_answers(  # type: ignore
        start=start,
        end=end,
        probabilities=probabilities,
        flattened_documents=example_documents[0],
        queries=example_queries,
        answers_per_seq=5,
        top_k=3,
        score_threshold=None,
        query_ids=query_ids,
        document_ids=document_ids,
        no_answer=True,
        overlap_threshold=None,
    )
    expected_no_answers = [0.2 * 0.16 * 0.12, 0]
    for query, answers, expected_no_answer, probabilities in zip(
        example_queries, nested_answers, expected_no_answers, [probabilities[:3, -1], probabilities[3:, -1]]
    ):
        assert len(answers) == 4
        for doc, answer, score in zip(example_documents[0], reversed(answers[:3]), probabilities):
            assert answer.query == query
            assert answer.document == doc
            assert answer.score == pytest.approx(score)
            if "page_number" in doc.meta:
                assert answer.meta["answer_page_number"] == doc.meta["page_number"]
        no_answer = answers[-1]
        assert no_answer.query == query
        assert no_answer.document is None
        assert no_answer.score == pytest.approx(expected_no_answer)


def test_add_answer_page_number_returns_same_answer(mock_reader: ExtractiveReader, caplog):
    # answer.document_offset is None
    document = Document(content="I thought a lot about this. The answer is 42.", meta={"page_number": 5})
    answer = ExtractedAnswer(
        data="42",
        query="What is the answer?",
        document=document,
        score=1.0,
        document_offset=None,
        meta={"meta_key": "meta_value"},
    )
    assert mock_reader._add_answer_page_number(answer=answer) == answer

    # answer.document is None
    answer = ExtractedAnswer(
        data="42",
        query="What is the answer?",
        document=None,
        score=1.0,
        document_offset=ExtractedAnswer.Span(42, 44),
        meta={"meta_key": "meta_value"},
    )
    assert mock_reader._add_answer_page_number(answer=answer) == answer

    # answer.document.meta is None
    document = Document(content="I thought a lot about this. The answer is 42.")
    answer = ExtractedAnswer(
        data="42",
        query="What is the answer?",
        document=document,
        score=1.0,
        document_offset=ExtractedAnswer.Span(42, 44),
        meta={"meta_key": "meta_value"},
    )
    assert mock_reader._add_answer_page_number(answer=answer) == answer

    # answer.document.meta["page_number"] is not int
    document = Document(content="I thought a lot about this. The answer is 42.", meta={"page_number": "5"})
    answer = ExtractedAnswer(
        data="42",
        query="What is the answer?",
        document=document,
        score=1.0,
        document_offset=ExtractedAnswer.Span(42, 44),
        meta={"meta_key": "meta_value"},
    )
    with caplog.at_level(logging.WARNING):
        assert mock_reader._add_answer_page_number(answer=answer) == answer
        assert "page_number must be int" in caplog.text


def test_add_answer_page_number_with_form_feed(mock_reader: ExtractiveReader):
    document = Document(
        content="I thought a lot about this. \f And this document is long. \f The answer is 42.",
        meta={"page_number": 5},
    )
    answer = ExtractedAnswer(
        data="42",
        query="What is the answer?",
        document=document,
        context="The answer is 42.",
        score=1.0,
        document_offset=ExtractedAnswer.Span(73, 75),
        context_offset=ExtractedAnswer.Span(14, 16),
        meta={"meta_key": "meta_value"},
    )
    answer_with_page_number = mock_reader._add_answer_page_number(answer=answer)
    assert answer_with_page_number.meta["answer_page_number"] == 7


@patch("haystack.components.readers.extractive.AutoTokenizer.from_pretrained")
@patch("haystack.components.readers.extractive.AutoModelForQuestionAnswering.from_pretrained")
def test_warm_up_use_hf_token(mocked_automodel, mocked_autotokenizer, initialized_token: Secret):
    reader = ExtractiveReader("deepset/roberta-base-squad2", device=ComponentDevice.from_str("cpu"))

    class MockedModel:
        def __init__(self):
            self.hf_device_map = {"": "cpu"}

    mocked_automodel.return_value = MockedModel()
    reader.warm_up()

    mocked_automodel.assert_called_once_with("deepset/roberta-base-squad2", token="secret-token", device_map="cpu")
    mocked_autotokenizer.assert_called_once_with("deepset/roberta-base-squad2", token="secret-token")


@patch("haystack.components.readers.extractive.AutoTokenizer.from_pretrained")
@patch("haystack.components.readers.extractive.AutoModelForQuestionAnswering.from_pretrained")
def test_device_map_auto(mocked_automodel, _mocked_autotokenizer, monkeypatch):
    monkeypatch.delenv("HF_API_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    reader = ExtractiveReader("deepset/roberta-base-squad2", model_kwargs={"device_map": "auto"})
    auto_device = ComponentDevice.resolve_device(None)

    class MockedModel:
        def __init__(self):
            self.hf_device_map = {"": auto_device.to_hf()}

    mocked_automodel.return_value = MockedModel()
    reader.warm_up()

    mocked_automodel.assert_called_once_with("deepset/roberta-base-squad2", token=None, device_map="auto")
    assert reader.device == ComponentDevice.from_multiple(DeviceMap.from_hf({"": auto_device.to_hf()}))


@patch("haystack.components.readers.extractive.AutoTokenizer.from_pretrained")
@patch("haystack.components.readers.extractive.AutoModelForQuestionAnswering.from_pretrained")
def test_device_map_str(mocked_automodel, _mocked_autotokenizer, monkeypatch):
    monkeypatch.delenv("HF_API_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    reader = ExtractiveReader("deepset/roberta-base-squad2", model_kwargs={"device_map": "cpu:0"})

    class MockedModel:
        def __init__(self):
            self.hf_device_map = {"": "cpu:0"}

    mocked_automodel.return_value = MockedModel()
    reader.warm_up()

    mocked_automodel.assert_called_once_with("deepset/roberta-base-squad2", token=None, device_map="cpu:0")
    assert reader.device == ComponentDevice.from_multiple(DeviceMap.from_hf({"": "cpu:0"}))


@patch("haystack.components.readers.extractive.AutoTokenizer.from_pretrained")
@patch("haystack.components.readers.extractive.AutoModelForQuestionAnswering.from_pretrained")
def test_device_map_dict(mocked_automodel, _mocked_autotokenizer, monkeypatch):
    monkeypatch.delenv("HF_API_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    reader = ExtractiveReader(
        "deepset/roberta-base-squad2", model_kwargs={"device_map": {"layer_1": 1, "classifier": "cpu"}}
    )

    class MockedModel:
        def __init__(self):
            self.hf_device_map = {"layer_1": 1, "classifier": "cpu"}

    mocked_automodel.return_value = MockedModel()
    reader.warm_up()

    mocked_automodel.assert_called_once_with(
        "deepset/roberta-base-squad2", token=None, device_map={"layer_1": 1, "classifier": "cpu"}
    )
    assert reader.device == ComponentDevice.from_multiple(DeviceMap.from_hf({"layer_1": 1, "classifier": "cpu"}))


def test_device_map_and_device_warning(caplog):
    with caplog.at_level(logging.WARNING):
        _ = ExtractiveReader(
            "deepset/roberta-base-squad2", model_kwargs={"device_map": "cpu"}, device=ComponentDevice.from_str("cuda")
        )
        assert (
            "The parameters `device` and `device_map` from `model_kwargs` are both provided. Ignoring `device` "
            "and using `device_map`." in caplog.text
        )


class TestDeduplication:
    @pytest.fixture
    def doc1(self):
        return Document(content="I want to go to the river in Maine.")

    @pytest.fixture
    def doc2(self):
        return Document(content="I want to go skiing in Colorado.")

    @pytest.fixture
    def candidate_answer(self, doc1):
        answer1 = "the river"
        return ExtractedAnswer(
            query="test",
            data=answer1,
            document=doc1,
            document_offset=ExtractedAnswer.Span(doc1.content.find(answer1), doc1.content.find(answer1) + len(answer1)),
            score=0.1,
            meta={},
        )

    def test_calculate_overlap(self, mock_reader: ExtractiveReader, doc1: Document):
        answer1 = "the river"
        answer2 = "river in Maine"
        overlap_in_characters = mock_reader._calculate_overlap(
            answer1_start=doc1.content.find(answer1),
            answer1_end=doc1.content.find(answer1) + len(answer1),
            answer2_start=doc1.content.find(answer2),
            answer2_end=doc1.content.find(answer2) + len(answer2),
        )
        assert overlap_in_characters == 5

    def test_should_keep_false(
        self, mock_reader: ExtractiveReader, doc1: Document, doc2: Document, candidate_answer: ExtractedAnswer
    ):
        answer2 = "river in Maine"
        answer3 = "skiing in Colorado"
        keep = mock_reader._should_keep(
            candidate_answer=candidate_answer,
            current_answers=[
                ExtractedAnswer(
                    query="test",
                    data=answer2,
                    document=doc1,
                    document_offset=ExtractedAnswer.Span(
                        doc1.content.find(answer2), doc1.content.find(answer2) + len(answer2)
                    ),
                    score=0.1,
                    meta={},
                ),
                ExtractedAnswer(
                    query="test",
                    data=answer3,
                    document=doc2,
                    document_offset=ExtractedAnswer.Span(
                        doc2.content.find(answer3), doc2.content.find(answer3) + len(answer3)
                    ),
                    score=0.1,
                    meta={},
                ),
            ],
            overlap_threshold=0.01,
        )
        assert keep is False

    def test_should_keep_true(
        self, mock_reader: ExtractiveReader, doc1: Document, doc2: Document, candidate_answer: ExtractedAnswer
    ):
        answer2 = "Maine"
        answer3 = "skiing in Colorado"
        keep = mock_reader._should_keep(
            candidate_answer=candidate_answer,
            current_answers=[
                ExtractedAnswer(
                    query="test",
                    data=answer2,
                    document=doc1,
                    document_offset=ExtractedAnswer.Span(
                        doc1.content.find(answer2), doc1.content.find(answer2) + len(answer2)
                    ),
                    score=0.1,
                    meta={},
                ),
                ExtractedAnswer(
                    query="test",
                    data=answer3,
                    document=doc2,
                    document_offset=ExtractedAnswer.Span(
                        doc2.content.find(answer3), doc2.content.find(answer3) + len(answer3)
                    ),
                    score=0.1,
                    meta={},
                ),
            ],
            overlap_threshold=0.01,
        )
        assert keep is True

    def test_should_keep_missing_document_current_answer(
        self, mock_reader: ExtractiveReader, doc1: Document, candidate_answer: ExtractedAnswer
    ):
        answer2 = "river in Maine"
        keep = mock_reader._should_keep(
            candidate_answer=candidate_answer,
            current_answers=[
                ExtractedAnswer(
                    query="test",
                    data=answer2,
                    document=None,
                    document_offset=ExtractedAnswer.Span(
                        doc1.content.find(answer2), doc1.content.find(answer2) + len(answer2)
                    ),
                    score=0.1,
                    meta={},
                )
            ],
            overlap_threshold=0.01,
        )
        assert keep is True

    def test_should_keep_missing_document_candidate_answer(
        self, mock_reader: ExtractiveReader, doc1: Document, candidate_answer: ExtractedAnswer
    ):
        answer2 = "river in Maine"
        keep = mock_reader._should_keep(
            candidate_answer=ExtractedAnswer(
                query="test",
                data=answer2,
                document=None,
                document_offset=ExtractedAnswer.Span(
                    doc1.content.find(answer2), doc1.content.find(answer2) + len(answer2)
                ),
                score=0.1,
                meta={},
            ),
            current_answers=[
                ExtractedAnswer(
                    query="test",
                    data=answer2,
                    document=doc1,
                    document_offset=ExtractedAnswer.Span(
                        doc1.content.find(answer2), doc1.content.find(answer2) + len(answer2)
                    ),
                    score=0.1,
                    meta={},
                )
            ],
            overlap_threshold=0.01,
        )
        assert keep is True

    def test_should_keep_missing_span(
        self, mock_reader: ExtractiveReader, doc1: Document, candidate_answer: ExtractedAnswer
    ):
        answer2 = "river in Maine"
        keep = mock_reader._should_keep(
            candidate_answer=candidate_answer,
            current_answers=[
                ExtractedAnswer(query="test", data=answer2, document=doc1, document_offset=None, score=0.1, meta={})
            ],
            overlap_threshold=0.01,
        )
        assert keep is True

    def test_deduplicate_by_overlap_none_overlap(
        self, mock_reader: ExtractiveReader, candidate_answer: ExtractedAnswer
    ):
        result = mock_reader.deduplicate_by_overlap(
            answers=[candidate_answer, candidate_answer], overlap_threshold=None
        )
        assert len(result) == 2

    def test_deduplicate_by_overlap(
        self, mock_reader: ExtractiveReader, candidate_answer: ExtractedAnswer, doc1: Document
    ):
        answer2 = "Maine"
        extracted_answer2 = ExtractedAnswer(
            query="test",
            data=answer2,
            document=doc1,
            document_offset=ExtractedAnswer.Span(doc1.content.find(answer2), doc1.content.find(answer2) + len(answer2)),
            score=0.1,
            meta={},
        )
        result = mock_reader.deduplicate_by_overlap(
            answers=[candidate_answer, candidate_answer, extracted_answer2], overlap_threshold=0.01
        )
        assert len(result) == 2


@pytest.mark.integration
@pytest.mark.slow
def test_t5(monkeypatch):
    monkeypatch.delenv("HF_API_TOKEN", raising=False)  # https://github.com/deepset-ai/haystack/issues/8811
    reader = ExtractiveReader("sjrhuschlee/flan-t5-base-squad2")
    reader.warm_up()
    answers = reader.run(example_queries[0], example_documents[0], top_k=2)[
        "answers"
    ]  # remove indices when batching support is reintroduced
    assert answers[0].data == "Olaf Scholz"
    assert answers[0].score == pytest.approx(0.8085031509399414, abs=1e-5)
    assert answers[1].data == "Angela Merkel"
    assert answers[1].score == pytest.approx(0.8021242618560791, abs=1e-5)
    assert answers[2].data is None
    assert answers[2].score == pytest.approx(0.0378925803599941, abs=1e-5)
    assert len(answers) == 3
    # Uncomment assertions below when batching is reintroduced
    # assert answers[0][2].score == pytest.approx(0.051331606147570596)
    # assert answers[1][0].data == "Jerry"
    # assert answers[1][0].score == pytest.approx(0.7413333654403687)
    # assert answers[1][1].data == "Olaf Scholz"
    # assert answers[1][1].score == pytest.approx(0.7266613841056824)
    # assert answers[1][2].data is None
    # assert answers[1][2].score == pytest.approx(0.0707035798685709)


@pytest.mark.integration
@pytest.mark.slow
def test_roberta(monkeypatch):
    monkeypatch.delenv("HF_API_TOKEN", raising=False)  # https://github.com/deepset-ai/haystack/issues/8811
    reader = ExtractiveReader("deepset/tinyroberta-squad2")
    reader.warm_up()
    answers = reader.run(example_queries[0], example_documents[0], top_k=2)[
        "answers"
    ]  # remove indices when batching is reintroduced
    assert answers[0].data == "Olaf Scholz"
    assert answers[0].score == pytest.approx(0.8614975214004517)
    assert answers[1].data == "Angela Merkel"
    assert answers[1].score == pytest.approx(0.857952892780304)
    assert answers[2].data is None
    assert answers[2].score == pytest.approx(0.019673851661650588, abs=1e-5)
    assert len(answers) == 3
    # uncomment assertions below when there is batching in v2
    # assert answers[0][0].data == "Olaf Scholz"
    # assert answers[0][0].score == pytest.approx(0.8614975214004517)
    # assert answers[0][1].data == "Angela Merkel"
    # assert answers[0][1].score == pytest.approx(0.857952892780304)
    # assert answers[0][2].data is None
    # assert answers[0][2].score == pytest.approx(0.0196738764278237)
    # assert answers[1][0].data == "Jerry"
    # assert answers[1][0].score == pytest.approx(0.7048940658569336)
    # assert answers[1][1].data == "Olaf Scholz"
    # assert answers[1][1].score == pytest.approx(0.6604189872741699)
    # assert answers[1][2].data is None
    # assert answers[1][2].score == pytest.approx(0.1002123719777046)


@pytest.mark.integration
@pytest.mark.slow
def test_matches_hf_pipeline(monkeypatch):
    monkeypatch.delenv("HF_API_TOKEN", raising=False)  # https://github.com/deepset-ai/haystack/issues/8811
    reader = ExtractiveReader(
        "deepset/tinyroberta-squad2", device=ComponentDevice.from_str("cpu"), overlap_threshold=None
    )
    reader.warm_up()
    answers = reader.run(example_queries[0], [[example_documents[0][0]]][0], top_k=20, no_answer=False)[
        "answers"
    ]  # [0] Remove first two indices when batching support is reintroduced
    pipe = pipeline("question-answering", model=reader.model, tokenizer=reader.tokenizer, align_to_words=False)
    answers_hf = pipe(
        question=example_queries[0],
        context=example_documents[0][0].content,
        max_answer_len=1_000,
        handle_impossible_answer=False,
        top_k=20,
    )  # We need to disable HF postprocessing features to make the results comparable. This is related to https://github.com/huggingface/transformers/issues/26286
    assert len(answers) == len(answers_hf) == 20
    for answer, answer_hf in zip(answers, answers_hf):
        assert answer.document_offset.start == answer_hf["start"]
        assert answer.document_offset.end == answer_hf["end"]
        assert answer.data == answer_hf["answer"]
