from startlux.scenarios.genshin.desktop import main, emit

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit({"status": "error", "error_type": type(error).__name__})
        raise SystemExit(1)
