from message_services.discord_service import run_discord_client
import sys, codecs

sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
chatBot = {
    "name": "Coddy",
}

def main():
    run_discord_client(chatBot)

if __name__ == "__main__":
    main()



