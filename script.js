const chatForm = document.querySelector("#chatForm");
const messageInput = document.querySelector("#messageInput");
const chatMessages = document.querySelector("#chatMessages");

const history = [];

function addMessage(role, content, extraClass = "") {
  const message = document.createElement("article");
  message.className = `message ${role === "user" ? "user" : "bot"} ${extraClass}`.trim();

  const text = document.createElement("p");
  text.textContent = content;
  message.appendChild(text);

  chatMessages.appendChild(message);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return message;
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const message = messageInput.value.trim();
  if (!message) return;

  addMessage("user", message);
  history.push({ role: "user", content: message });
  messageInput.value = "";
  messageInput.focus();

  const button = chatForm.querySelector("button");
  button.disabled = true;
  const loadingMessage = addMessage("assistant", "Ved is thinking...", "loading");

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        message,
        history: history.slice(0, -1),
      }),
    });

    const data = await response.json();
    loadingMessage.remove();

    if (!response.ok) {
      throw new Error(data.error || "Something went wrong.");
    }

    addMessage("assistant", data.reply);
    history.push({ role: "assistant", content: data.reply });
  } catch (error) {
    loadingMessage.remove();
    addMessage("assistant", error.message);
  } finally {
    button.disabled = false;
  }
});