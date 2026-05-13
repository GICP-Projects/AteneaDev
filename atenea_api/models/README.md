# Models Directory

## Overview

This directory, `models`, is a central repository for all the trained models used throughout the `atenea_api`. It serves as a fixed location to store and organize various machine learning models, ensuring easy access and management.

The atenea_api project will have a setting `MODELS_DIR` defined which will point to this directory to access easily any model from anywhere.

## List of models to store

Below is the list of models that need to be **downloaded and stored** in this directory for the correct start-up of the project:


1. **Efficient Language Detector (ELD)**:

   - **Filename**: None `pip install eld`
   - **Source**: [ELD](https://github.com/nitotm/efficient-language-detector-py)
   - **Description**: Efficient language detector (Nito-ELD or ELD) is a fast and accurate language detector, is one of the fastest non compiled detectors, while its accuracy is within the range of the heaviest and slowest detectors.

2. **(DEPRECTATED) FastText Language Identification Model**:

   - **Deprecation Notice**: 
     - This model is deprecated from the project because it is no longer supported and is incompatible with Numpy 2.0.

   - **Filename**: `lid.176.bin` (131.3 MB)
   - **Source**: [FastText Supervised Models](https://fasttext.cc/docs/en/language-identification.html)
   - **Description**: This model is used for language identification, capable of distinguishing between 176 languages. It's based on the FastText library developed by Facebook AI Research.

    - References:

        ```
            @article{
                joulin2016bag,
                title={Bag of Tricks for Efficient Text Classification},
                author={Joulin, Armand and Grave, Edouard and Bojanowski, Piotr and Mikolov, Tomas},
                journal={arXiv preprint arXiv:1607.01759},
                year={2016}
            }

        ```
        ```
            @article{
                joulin2016fasttext,
                title={FastText.zip: Compressing text classification models},
                author={Joulin, Armand and Grave, Edouard and Bojanowski, Piotr and Douze, Matthijs and J{\'e}gou, H{\'e}rve and Mikolov, Tomas},
                journal={arXiv preprint arXiv:1612.03651},
                year={2016}
            }

        ```

## Contributing

If you are adding a new model to this directory, please update this README with the relevant details about the model, including its filename, source, and a brief description of its purpose and capabilities.
