import uvicorn
uvicorn.run('city_sim.api:app',host='0.0.0.0',port=8008,reload=False)
