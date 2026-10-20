const fileInput = document.getElementById("fileInput");
const uploadButton = document.getElementById("uploadButton");
const selectedFile = document.getElementById("selectedFile");
const result = document.getElementById("result");


fileInput.addEventListener("change", function () {

    const file = fileInput.files[0];

    if (file) {
        selectedFile.textContent = `Selected: ${file.name}`;
    }

});


uploadButton.addEventListener("click", async function () {

    const file = fileInput.files[0];

    if (!file) {
        result.textContent = "Please choose a file.";
        return;
    }

    const formData = new FormData();

    formData.append("file", file);

    result.textContent = "Uploading...";

    try {

        const response = await fetch(
            "http://127.0.0.1:8000/upload",
            {
                method: "POST",
                body: formData
            }
        );

        const data = await response.json();

        if (data.status === "Valid" || data.status === "Extracted") {

            result.textContent =
                `✅ ${data.message}`;

        } else {

            result.textContent =
                `❌ ${data.message}`;

        }

    } catch (error) {

        result.textContent =
            "❌ Could not connect to the backend.";

        console.error(error);
    }

});
