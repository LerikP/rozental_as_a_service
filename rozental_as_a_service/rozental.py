import collections
import functools
import logging
import math
import multiprocessing
import os
import re
import sys
from typing import List, Callable, DefaultDict, Tuple

from tabulate import tabulate

from rozental_as_a_service.args_utils import parse_args, prepare_arguments
from rozental_as_a_service.common_types import TypoInfo, BackendsConfig
from rozental_as_a_service.config import DEFAULT_WORDS_CHUNK_SIZE
from rozental_as_a_service.list_utils import chunks, flat
from rozental_as_a_service.text_utils import is_camel_case_word, split_camel_case_words
from rozental_as_a_service.typos_backends import (
    process_with_vocabulary, process_with_ya_speller,
    process_with_db_with_cache,
)
from rozental_as_a_service.files_utils import get_all_filepathes_recursively, get_content_from_file
from rozental_as_a_service.strings_extractors import (
    extract_from_python_src, extract_from_markdown, extract_from_html,
    extract_from_js,
    extract_from_po)

log = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
logging.getLogger('urllib3').setLevel(logging.INFO)


def extract_all_constants_from_path(
    path: str,
    exclude: List[str],
    process_dots: bool,
    processes_amount: int,
) -> List[str]:
    extractors = [
        (extract_from_python_src, ['py', 'pyi']),
        (extract_from_markdown, ['md']),
        (extract_from_html, ['html']),
        (extract_from_js, ['js', 'ts', 'tsx']),
        (extract_from_po, ['po']),
    ]

    extension_to_extractor_mapping: DefaultDict[str, List[Callable]] = collections.defaultdict(list)
    for extractor, extensions in extractors:
        for extension in extensions:
            extension_to_extractor_mapping[extension].append(extractor)

    string_constants: List[str] = []

    for extension, extension_extractors in extension_to_extractor_mapping.items():
        if os.path.isdir(path):
            all_files = get_all_filepathes_recursively(path, exclude, extension)
        else:
            all_files = [path] if path.endswith(extension) else []
        if not process_dots:
            all_files = [f for f in all_files if '/.' not in f and not f.startswith('.')]
        if not all_files:
            continue
        chunk_size = math.ceil(len(all_files) / processes_amount)
        new_strings = multiprocessing.Pool(processes_amount).map(
            functools.partial(extract_all_constants_from_files, extractors=extension_extractors),
            chunks(all_files, chunk_size),
        )
        string_constants += flat(new_strings)
    return list(set(string_constants))


def extract_all_constants_from_files(files_pathes: List[str], extractors: List[Callable]) -> List[str]:
    string_constants: List[str] = []
    for filepath in files_pathes:
        for extractor_callable in extractors:
            log.debug(f'Start reading {filepath}...')
            raw_content = get_content_from_file(filepath, guess_encoding=False)
            if raw_content is None:
                raw_content = get_content_from_file(filepath, guess_encoding=True)
            if raw_content is None:
                return []
            log.debug(f'Start processing {filepath}...')
            string_constants += extractor_callable(raw_content)
    return extract_words(list(set(string_constants)))


def fetch_typos_info(string_constants: List[str], vocabulary_path: str = None, db_path: str = None) -> List[TypoInfo]:
    typos_info: List[TypoInfo] = []

    backends = [
        process_with_vocabulary,
        process_with_db_with_cache,
        process_with_ya_speller,
    ]
    backend_config: BackendsConfig = {
        'vocabulary_path': vocabulary_path,
        'db_path': db_path,
        'speller_chunk_size': DEFAULT_WORDS_CHUNK_SIZE,
    }
    for words_chunk in chunks(string_constants, backend_config['speller_chunk_size']):
        for words_processor in backends:
            sure_correct, sure_with_typo_info, unknown = words_processor(words_chunk, backend_config)
            typos_info += sure_with_typo_info
            # переопределяем переменную цикла так, чтобы следующему процессору доставались
            # только слова, по которым не известно, ок ли они
            words_chunk = unknown

    return typos_info


def extract_words(
    raw_constants: List[str],
    min_word_length: int = 3,
    only_russian: bool = True,
    strip_urls: bool = True,
) -> List[str]:
    common_replacements = [
        # remove diacritic symbols manually
        ('\u0306', ''),
        ('\u0301', ''),
        ('\u0300', ''),
    ]
    url_regexp = r'(http(s)?:\/\/.)?(www\.)?[-a-zA-Z0-9@:%._\+~#=]{2,256}\.[a-z]{2,6}\b([-a-zA-Z0-9@:%_\+.~#?&//=]*)'
    processed_words: List[str] = []
    raw_camelcase_words: List[str] = []
    for constant in raw_constants:
        if strip_urls:
            constant = re.sub(url_regexp, ' ', constant)

        for replace_from, replace_to in common_replacements:
            constant = constant.replace(replace_from, replace_to)

        new_processed_words, new_camelcase_words = process_raw_constant(constant, min_word_length)
        processed_words += new_processed_words
        raw_camelcase_words += new_camelcase_words
    processed_words += process_camelcased_words(raw_camelcase_words)

    processed_words = list(set(processed_words))

    word_regexp = r'[а-яё-]+' if only_russian else r'[а-яёa-z-]+'
    filtered_words = []
    for word in processed_words:
        match = re.match(word_regexp, word)
        if match:
            word = match.group()
            if 'а-я' not in word and 'a-z' not in word:  # most likely regexp
                filtered_words.append(word)
    processed_words = filtered_words
    return processed_words


def process_raw_constant(constant: str, min_word_length: int) -> Tuple[List[str], List[str]]:
    processed_words: List[str] = []
    raw_camelcase_words: List[str] = []
    for raw_word in re.findall(r'[\w-]+', constant):
        word = raw_word.strip()
        if (
            len(word) >= min_word_length
            and not (word.startswith('-') or word.endswith('-'))
        ):
            if is_camel_case_word(word):
                raw_camelcase_words.append(word)
            else:
                processed_words.append(word.lower())
    return processed_words, raw_camelcase_words


def process_camelcased_words(raw_camelcase_words: List[str]) -> List[str]:
    processed_words: List[str] = []
    for camel_case_words in raw_camelcase_words:
        splitted_words = split_camel_case_words(camel_case_words)
        if splitted_words:
            processed_words += splitted_words
    return processed_words


def reorder_vocabulary(vocabulary_path: str) -> None:
    with open(vocabulary_path, 'r') as file_handler:
        raw_lines = file_handler.readlines()
    sections: List[List[str]] = []
    current_section: List[str] = []
    for line in raw_lines:
        processed_line = line.strip()
        if not processed_line:
            continue
        if processed_line.startswith('#') and current_section:
            sections.append(current_section)
            current_section = []
        current_section.append(processed_line)
    if current_section:
        sections.append(current_section)
    sorted_sections: List[List[str]] = []
    for section_num, section in enumerate(sections, 1):
        sorted_sections.append(
            [f'{l}\n' for l in section if l.startswith('#')]
            + sorted(f'{l}\n' for l in section if not l.startswith('#'))
            + (['\n'] if section_num < len(sections) else []),
        )

    with open(vocabulary_path, 'w') as file_handler:
        file_handler.writelines(flat(sorted_sections))


def main() -> None:
    script_arguments = parse_args()
    arguments = prepare_arguments(script_arguments)

    log.setLevel(max(3 - arguments['verbosity'], 0) * 10)

    log.debug(f'Starting with following parameters: {arguments}')

    unique_words = extract_all_constants_from_path(
        arguments['path'],
        arguments['exclude'],
        arguments['process_dots'],
        arguments['processes_amount'],
    )
    typos_info = fetch_typos_info(unique_words, arguments['vocabulary_path'], arguments['db_path'])

    if arguments['reorder_vocabulary'] and os.path.exists(arguments['vocabulary_path']):
        reorder_vocabulary(arguments['vocabulary_path'])

    if typos_info:
        table = [(t['original'], ', '.join(t['possible_options'])) for t in typos_info]
        print(tabulate(table, headers=('Найденное слово', 'Возможные исправления')))  # noqa
        if not arguments['exit_zero']:
            exit(1)


if __name__ == '__main__':
    main()
