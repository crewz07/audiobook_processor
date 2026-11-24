# ffmpeg command: ffmpeg -f concat -safe 0 -i list.txt -vn -y -b:a 320k -acodec aac -ac 2 result.m4a*
import os
folder_path = "."
list_path = "list.txt"
if __name__ == "__main__":
    try:
        os.remove(list_path)
    except FileNotFoundError:
        pass
    i = 1
    file_path = os.path.join(folder_path, f"ch{i:02}.m4b")
    print(file_path)
    with open(list_path, "w") as list_file:
        while os.path.exists(file_path):
            list_file.write(f"file ch{i:02}.m4b\n")
            i += 1
            file_path = os.path.join(folder_path, f"ch{i:02}.m4b")
            print(file_path)
