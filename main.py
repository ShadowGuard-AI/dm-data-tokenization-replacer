import argparse
import csv
import logging
import uuid
import os
from collections import defaultdict
from typing import Dict, Callable

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def setup_argparse():
    """
    Sets up the argument parser for the command-line interface.
    """
    parser = argparse.ArgumentParser(
        description="Replaces sensitive data in a CSV file with unique, reversible tokens."
    )
    parser.add_argument("input_file", help="The input CSV file.")
    parser.add_argument("output_file", help="The output CSV file with tokenized data.")
    parser.add_argument(
        "--tokenize_columns",
        nargs="+",
        required=True,
        help="A list of column names to tokenize."
    )
    parser.add_argument(
        "--token_method",
        choices=["uuid", "sequential"],
        default="uuid",
        help="The tokenization method to use (uuid or sequential). Defaults to uuid."
    )
    parser.add_argument(
        "--token_map_file",
        default="token_map.csv",
        help="The file to store the token-to-value mapping (CSV). Defaults to token_map.csv."
    )
    parser.add_argument(
        "--detokenize",
        action="store_true",
        help="Detokenize the data using the token map file."
    )
    return parser

def generate_uuid_token(value: str, token_map: Dict[str, str]) -> str:
    """
    Generates a UUID token for a given value, ensuring uniqueness.
    """
    # Attempt to retrieve existing token.
    for token, original_value in token_map.items():
        if original_value == value:
            return token

    token = str(uuid.uuid4())
    #Prevent collision, try again if needed
    while token in token_map:
        token = str(uuid.uuid4())

    return token
    

def generate_sequential_token(value: str, token_map: Dict[str, str]) -> str:
     """
     Generates a sequential token for a given value.
     This is more complex and needs to manage a counter.
     """

     # Attempt to retrieve existing token.
     for token, original_value in token_map.items():
         if original_value == value:
             return token

     # Find the next available sequential ID
     next_id = 1
     while str(next_id) in token_map:
         next_id += 1
     token = str(next_id)
     return token

def tokenize_data(
    input_file: str,
    output_file: str,
    tokenize_columns: list[str],
    token_method: str,
    token_map_file: str
) -> None:
    """
    Tokenizes specified columns in a CSV file and saves the result to a new file.
    """
    token_map: Dict[str, str] = {}
    # One map serves every tokenized column in the file. The same value in
    # two different columns earns the same token, so equality leaks across
    # columns: a reader can tell a name cell and a city cell hold the same
    # string without holding the map. I keep the single map because it is
    # what makes a value tokenize the same way everywhere it appears, and
    # that trade should be written down where the map is created.

    # Read existing token map, if it exists
    if os.path.exists(token_map_file):
        try:
            with open(token_map_file, "r", newline="") as csvfile:
                reader = csv.reader(csvfile)
                for row in reader:
                    if len(row) == 2:
                        token_map[row[0]] = row[1]
                    else:
                        logging.warning(f"Skipping malformed row in token map file: {row}")

        except Exception as e:
            logging.error(f"Error reading existing token map: {e}")
            raise

    if token_method == "uuid":
        token_generator: Callable[[str, Dict[str, str]], str] = generate_uuid_token
    elif token_method == "sequential":
        token_generator: Callable[[str, Dict[str, str]], str] = generate_sequential_token
    else:
        raise ValueError(f"Invalid token_method: {token_method}")


    try:
        with open(input_file, "r", newline="") as infile, open(
            output_file, "w", newline=""
        ) as outfile:
            reader = csv.DictReader(infile)
            writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
            writer.writeheader()

            for row in reader:
                for column in tokenize_columns:
                    if column in row:
                        original_value = row[column]
                        # This lookup walks the whole map for every cell,
                        # and the generator below walks it again for a new
                        # value. Honest cost: the run slows in proportion to
                        # rows times distinct values. A reversed dictionary
                        # would fix it, and keeping two maps in sync is the
                        # kind of change I do not smuggle into this pass.
                        token = next((k for k, v in token_map.items() if v == original_value), None)
                        if token is None: #Create a new token if it does not exist
                            token = token_generator(original_value, token_map)
                            token_map[token] = original_value
                        row[column] = token
                    else:
                        logging.warning(f"Column '{column}' not found in input file.")

                writer.writerow(row)
    except FileNotFoundError:
        logging.error(f"Input file '{input_file}' not found.")
        raise
    except Exception as e:
        logging.error(f"An error occurred during tokenization: {e}")
        raise

    # The map is written only after the output file has closed, and it is
    # rewritten whole. That order has a sharp edge: a crash during the row
    # loop leaves a partial output whose new tokens exist in no map, so
    # those rows can never be detokenized. The map on disk always describes
    # the last completed run, never the partial one.
    try:
        with open(token_map_file, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            for token, value in token_map.items():
                writer.writerow([token, value])
    except Exception as e:
        logging.error(f"Error writing token map to file: {e}")
        raise


def detokenize_data(input_file: str, output_file: str, token_map_file: str, tokenize_columns: list[str]) -> None:
    """
    Detokenizes the tokenized columns in a CSV file using a token map and saves the result to a new file.
    """
    token_map: Dict[str, str] = {}
    try:
        with open(token_map_file, "r", newline="") as csvfile:
            reader = csv.reader(csvfile)
            for row in reader:
                if len(row) == 2:
                    token_map[row[0]] = row[1]
                else:
                    logging.warning(f"Skipping malformed row in token map file: {row}")
    except FileNotFoundError:
        logging.error(f"Token map file '{token_map_file}' not found.")
        raise
    except Exception as e:
        logging.error(f"Error reading token map file: {e}")
        raise

    try:
        with open(input_file, "r", newline="") as infile, open(
            output_file, "w", newline=""
        ) as outfile:
            reader = csv.DictReader(infile)
            writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
            writer.writeheader()

            for row in reader:
                # Replace in the tokenized columns only. Sequential tokens are
                # small integers ("1", "2", ...), so scanning every column
                # rewrote unrelated cells that happened to hold the same
                # string: a code column containing 1 came back as the SSN
                # that token 1 mapped to.
                for column in tokenize_columns:
                    # A cell whose token is missing from the map passes
                    # through unchanged, with no warning. Detokenizing with
                    # the wrong map file therefore produces a plausible
                    # looking file that is still tokenized, and nothing in
                    # the output says which cells were left behind.
                    if column in row and row[column] in token_map:
                        row[column] = token_map[row[column]]
                writer.writerow(row)
    except FileNotFoundError:
        logging.error(f"Input file '{input_file}' not found.")
        raise
    except Exception as e:
        logging.error(f"An error occurred during detokenization: {e}")
        raise


def main():
    """
    Main function to execute the data tokenization or detokenization process.
    """
    parser = setup_argparse()
    args = parser.parse_args()

    try:
        if args.detokenize:
            detokenize_data(args.input_file, args.output_file, args.token_map_file, args.tokenize_columns)
            logging.info(f"Data detokenized successfully. Output saved to '{args.output_file}'.")
        else:
            tokenize_data(
                args.input_file,
                args.output_file,
                args.tokenize_columns,
                args.token_method,
                args.token_map_file,
            )
            logging.info(
                f"Data tokenized successfully. Output saved to '{args.output_file}'. Token map saved to '{args.token_map_file}'."
            )
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        exit(1)


if __name__ == "__main__":
    main()