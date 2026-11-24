import os
import re
import sys
list_path = "list.txt"

if __name__ == "__main__":

    # get the command line argument which should Contain the Diretory of the Audiobook
    try:
        audio_book_directory = sys.argv[1]
    except IndexError:
        print(f"Missing Command Line Argument for Directory Name")
        exit()

    # remove old list.txt if it exists
    try:
        os.remove(list_path)
    except FileNotFoundError:
        pass

    # remove old list.txt if it exists
    try:
        os.remove(os.path.join(audio_book_directory, list_path))
    except FileNotFoundError:
        pass

    # change file names in the subdirectory
    list_txt = ""
    for file_name in os.listdir(audio_book_directory):
        if ".mp3" in file_name:
            chapter_number = int(re.search(r'\d+', file_name).group())
            print(f"ch{chapter_number:02}.mp3")

            # rename
            os.rename(os.path.join(audio_book_directory, file_name), os.path.join(audio_book_directory, f"ch{chapter_number:02}.mp3") )

            # create entry in list.txt
            list_txt += f"file ch{chapter_number:02}.mp3\n"
        elif ".m4b" in file_name:
            chapter_number = int(re.search(r'p(\d+)', file_name).group()[1])
            print(f"ch{chapter_number:02}.m4b")
            os.rename(os.path.join(audio_book_directory, file_name), os.path.join(audio_book_directory, f"ch{chapter_number:02}.m4b") )

            list_txt += f"file ch{chapter_number:02}.m4b\n"
        else:
            print("unknown file ending, skipping...")

    # output the list_txt to list.txt file
    with open(list_path, "w") as list_file:
        list_file.write(list_txt)

    # move the list file into the directory of the file location.
    os.rename(list_path, os.path.join(audio_book_directory, list_path))
