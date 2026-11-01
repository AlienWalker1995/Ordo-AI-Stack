# Forked from VibeVoice-ComfyUI (MIT licensed) by Fabio Sarracino
# Modified for Ordo-ComfyUI: updated class name and CATEGORY

import hashlib
import logging
import os

import folder_paths

# Setup logging
logger = logging.getLogger("Ordo")

class OrdoLoadTextFromFileNode:
    @classmethod
    def INPUT_TYPES(cls):
        all_files = []
        for dir_name in ["input", "output", "temp"]:
            files = cls.get_files_for_directory(dir_name)
            for f in files:
                if f != "No text files found":
                    all_files.append(f"{dir_name}/{f}")

        if not all_files:
            all_files = ["No text files found in any directory"]

        return {
            "required": {
                "file": (sorted(all_files), {
                    "tooltip": "Select a text file to load (format: directory/filename)"
                }),
            }
        }

    @classmethod
    def get_files_for_directory(cls, source_dir):
        if source_dir == "input":
            dir_path = folder_paths.get_input_directory()
        elif source_dir == "output":
            dir_path = folder_paths.get_output_directory()
        elif source_dir == "temp":
            dir_path = folder_paths.get_temp_directory()
        else:
            return []

        files = []
        try:
            for f in os.listdir(dir_path):
                if os.path.isfile(os.path.join(dir_path, f)):
                    if f.lower().endswith('.txt'):
                        files.append(f)
        except Exception as e:
            logger.warning(f"Error listing files in {source_dir}: {e}")

        return files

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "load_text"
    CATEGORY = "Ordo/TTS"
    DESCRIPTION = "Load text content from a .txt file"

    def load_text(self, file: str):
        try:
            if not file or file == "No text files found in any directory":
                raise Exception("Please select a valid text file.")

            if "/" not in file:
                raise Exception(f"Invalid file format: {file}")

            source_dir, filename = file.split("/", 1)

            if source_dir == "input":
                dir_path = folder_paths.get_input_directory()
            elif source_dir == "output":
                dir_path = folder_paths.get_output_directory()
            elif source_dir == "temp":
                dir_path = folder_paths.get_temp_directory()
            else:
                raise Exception(f"Invalid source directory: {source_dir}")

            file_path = os.path.join(dir_path, filename)

            if not os.path.exists(file_path):
                raise Exception(f"File not found: {file_path}")

            with open(file_path, encoding='utf-8') as f:
                text_content = f.read()

            if not text_content.strip():
                raise Exception("File is empty or contains only whitespace")

            return (text_content,)

        except UnicodeDecodeError as e:
            raise Exception(f"Encoding error reading file: {str(e)}. File may not be UTF-8 encoded.")
        except Exception as e:
            logger.error(f"Failed to load text file: {str(e)}")
            raise Exception(f"Error loading text file: {str(e)}")

    @classmethod
    def IS_CHANGED(cls, file):
        if not file or file == "No text files found in any directory":
            return "no_file"

        if "/" not in file:
            return f"{file}_invalid"

        source_dir, filename = file.split("/", 1)

        if source_dir == "input":
            dir_path = folder_paths.get_input_directory()
        elif source_dir == "output":
            dir_path = folder_paths.get_output_directory()
        elif source_dir == "temp":
            dir_path = folder_paths.get_temp_directory()
        else:
            return f"{file}_invalid_dir"

        file_path = os.path.join(dir_path, filename)

        if not os.path.exists(file_path):
            return f"{file}_not_found"

        try:
            m = hashlib.sha256()
            with open(file_path, 'rb') as f:
                m.update(f.read())
            return m.digest().hex()
        except Exception:
            return f"{file}_error"

    @classmethod
    def VALIDATE_INPUTS(cls, file, **kwargs):
        if not file or file == "No text files found in any directory":
            return "No valid text file selected"

        if "/" not in file:
            return f"Invalid file format: {file}"

        source_dir, filename = file.split("/", 1)

        if source_dir == "input":
            dir_path = folder_paths.get_input_directory()
        elif source_dir == "output":
            dir_path = folder_paths.get_output_directory()
        elif source_dir == "temp":
            dir_path = folder_paths.get_temp_directory()
        else:
            return f"Invalid source directory: {source_dir}"

        file_path = os.path.join(dir_path, filename)
        if not os.path.exists(file_path):
            return f"File not found: {filename} in {source_dir}"

        return True
